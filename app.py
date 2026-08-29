import os
import json
import time
import threading
import logging
import re
from datetime import date, datetime, timedelta

import gspread
import pytz
import requests
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials

from loseit_coaching import (
    build_food_coaching,
    build_food_ledger_coaching_data,
    build_weekly_health_report,
)
from food.database import (
    get_pending_unresolved_foods,
    get_portion_profile,
    get_unresolved_food,
    save_food_alias,
    save_portion_profile,
    save_unresolved_food,
    set_unresolved_food_status,
    update_unresolved_food_details,
)
from food.interpreter import (
    FoodInterpretation,
    clean_interpretation_missing_fields,
    interpret_food_message,
    normalize_signature_food,
)
from food.library import (
    add_food_with_nutrition,
    add_user_nutrition_version,
    archive_user_saved_food,
    list_nutrition_ready_foods,
    list_user_saved_foods,
    save_barcode_mapping,
    update_user_saved_food_identity,
)
from food.finder import (
    get_food_location,
    search_food_locations,
)
from food.health_check import (
    build_food_library_health_check,
    health_check_food_items,
    health_check_food_reason,
)
from food.cleanup import (
    consolidate_food_records,
    list_possible_food_duplicates,
    mark_foods_keep_separate,
)
from food.ledger import (
    add_food_entry,
    copy_food_entries_to_date,
    delete_food_favorite,
    delete_food_entry,
    find_recent_duplicate_entry,
    get_daily_totals,
    list_food_favorites,
    list_food_entries,
    save_food_favorite_from_entry,
    update_food_entry,
)
from food.goals import (
    archive_active_weight_goal,
    calculate_weight_goal,
    create_weight_goal,
    get_active_weight_goal,
    get_latest_weight_goal_calculation,
    list_weight_goals,
    save_weight_goal_calculation,
    update_active_weight_goal,
)
from food.pantry import (
    PANTRY_FOOD_CATEGORIES,
    PANTRY_STORAGE_AREAS,
    add_pantry_item,
    add_pantry_items,
    clean_pantry_name,
    clear_pantry,
    link_pantry_item_to_food,
    list_pantry_items,
    parse_pantry_item_list,
    remove_pantry_item,
    rename_pantry_item,
    unlink_pantry_item_nutrition,
    update_pantry_item_organization,
)
from food.pantry_advisor import (
    MAX_ADDITIONAL_INGREDIENTS,
    MEAL_CALORIE_LIMITS,
    build_pantry_goal_context,
    generate_pantry_meal_ideas,
    generate_smart_pantry_swaps,
    scale_pantry_meal_nutrition,
)
from food.pantry_photo import (
    MAX_PANTRY_PHOTO_SESSION_ITEMS,
    analyze_pantry_photo,
    merge_pantry_photo_names,
)
from food.shopping import (
    add_shopping_item,
    add_shopping_items,
    clear_shopping_list,
    get_shopping_item,
    list_shopping_items,
    mark_shopping_item_purchased,
    parse_shopping_item_list,
    remove_shopping_item,
)
from food.recipes import (
    calculate_recipe_nutrition,
    create_saved_recipe_from_ingredients,
    delete_saved_recipe,
    find_recipe_ingredient_food,
    get_saved_recipe,
    ingredient_serving_multiplier,
    list_recipe_ingredient_foods,
    list_recipe_pantry_foods,
    list_saved_recipes,
    prepare_recipe_ingredient,
    save_pantry_meal_idea,
    update_saved_recipe,
    update_saved_recipe_from_ingredients,
    update_saved_recipe_nutrition,
)
from food.recipe_importer import (
    parse_recipe_photo,
    parse_recipe_text,
    suggest_generic_ingredient_name,
)
from food.nutrition_provider import (
    lookup_official_nutrition,
    parse_serving_description,
)
from food.menu_photo_advisor import (
    analyze_food_photo,
    analyze_menu_photo,
    download_telegram_photo,
    format_food_photo_estimate,
    format_menu_photo_analysis,
    is_food_photo_request,
    midpoint_food_photo_nutrition,
    read_barcode_photo,
    read_nutrition_label_photo,
    refine_food_photo_estimate,
)
from food.barcode_provider import lookup_barcode_nutrition
from food.restaurant_advisor import recommend_restaurant_entrees
from food.usda_provider import normalize_barcode
from food.resolver import (
    is_trusted_saved_food,
    normalized_food_tokens,
    resolve_food,
)
from conversation_engine import (
    cancel_conversation,
    complete_conversation,
    get_active_conversation,
    start_conversation,
    update_conversation,
)
from memory.cases import (
    create_case,
    evaluate_missing_data_cases,
)
from health_insights import (
    build_daily_health_evidence,
    build_weekly_health_evidence,
    fallback_daily_health_narrative,
    fallback_weekly_health_narrative,
    format_daily_health_insight,
    format_weekly_health_insight,
    generate_daily_health_narrative,
    generate_weekly_health_narrative,
)

app = Flask(__name__)

TELEGRAM_UPDATE_LOCK = threading.Lock()

PACIFIC_TZ = pytz.timezone("US/Pacific")

CHAT_ID = os.getenv("HEALTH_CHAT_ID")
TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_TOKEN")
JSON_PATH = os.getenv("HEALTH_GOOGLE_JSON_PATH")

STATE_FILE = "/home/vandal/bots/healthcoach/logs/state.json"
LOG_FILE = "/home/vandal/bots/healthcoach/logs/healthcoach.log"
GOALS_FILE = "/home/vandal/bots/healthcoach/goals.json"
MEMORY_START_DATE = os.getenv("HEALTH_MEMORY_START_DATE", "")

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Timestamp",
    "Steps",
    "Total Cals",
    "Active Cals",
    "Sleep",
    "RHR",
    "Weight",
    "HRV",
    "Dietary Cals",
    "Protein",
    "Exercise Minutes",
    "Cardio Fitness",
    "Walking Heart Rate Average",
    "Blood Pressure Systolic",
    "Blood Pressure Diastolic",
    "Blood Pressure Measured At",
    "RHR Measured At",
    "HRV Measured At",
    "Cardio Fitness Measured At",
    "Walking Heart Rate Measured At",
]

TRACKER_LAST_COLUMN = "T"
MAX_WALKING_HEART_RATE_BPM = 300.0
MAX_BLOOD_PRESSURE_MMHG = 300.0

EARLY_PROTEIN_MEALS = {"breakfast", "school snack", "lunch"}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def reset_state_for_new_day(state, today_str):
    if state.get("date") != today_str:
        return {
            "date": today_str,
            "food_coaching_sent": False,
            "midday_sent": False,
            "evening_sent": False,
            "sleep_reminder_sent": False,
            "goal_check_sent": False,
            "weekly_sent": False,
            "library_health_check_reminder_sent": False,
            "telegram_update_offset": state.get("telegram_update_offset"),
        }
    return state


def load_goals():
    if not os.path.exists(GOALS_FILE):
        return {"active_goals": []}
    try:
        with open(GOALS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"active_goals": []}


def save_goals(data):
    with open(GOALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_active_goals():
    data = load_goals()
    return data.get("active_goals", [])


def initialize_goals_if_needed(reference_date):
    data = load_goals()
    goals = data.get("active_goals", [])
    changed = False

    for goal in goals:
        if goal.get("start_date") in ("", None):
            goal["start_date"] = reference_date.strftime("%Y-%m-%d")
            changed = True

    if changed:
        save_goals(data)


def menu_reply_markup(message):
    """Return an iPhone-friendly Telegram keyboard for menu messages."""
    one_time = False

    if "Undo this food entry?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif (
        (
            "Nutrition has not been looked up or saved yet." in message
            or message.startswith("Please reply:\n1. Correct")
        )
        and "1. Correct" in message
    ):
        rows = [["Correct", "Edit", "Cancel"]]
        one_time = True
    elif (
        (
            "Nothing has been logged yet." in message
            or message.startswith("Please reply:\n1. Log It")
        )
        and "1. Log It" in message
    ):
        rows = [
            ["Log It", "Enter custom nutrition"],
            ["Edit", "Cancel"],
        ]
        one_time = True
    elif (
        ("Unknown food " in message and "1. Enter nutrition" in message)
        or "Choose another option." in message
        or "food remains in the queue." in message
    ):
        rows = [
            ["Enter nutrition", "Edit"],
            ["Automatic lookup", "Skip for now"],
            ["Cancel this food"],
        ]
        one_time = True
    elif "1. Enter package label nutrition" in message:
        rows = [
            ["Enter package label nutrition"],
            ["Different description", "Save for later"],
            ["Cancel"],
        ]
        one_time = True
    elif (
        "Tell me any details that would improve this estimate."
        in message
    ):
        rows = [["Skip details"], ["Cancel"]]
        one_time = True
    elif "How much of the pictured food did you eat?" in message:
        rows = [
            ["All of it", "About 3/4"],
            ["About half", "Cancel"],
        ]
        one_time = True
    elif (
        "Which meal should this estimate be logged under?"
        in message
    ):
        rows = [
            ["Before breakfast", "Breakfast"],
            ["Morning snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Cancel"],
        ]
        one_time = True
    elif "Log this estimated meal?" in message:
        rows = [
            ["Log Estimate", "Change details"],
            ["Cancel"],
        ]
        one_time = True
    elif "Log another one?" in message:
        rows = [["Log another", "Keep first"]]
        one_time = True
    elif "1. Save" in message and "2. Edit" in message:
        rows = [["Save", "Edit", "Cancel"]]
        one_time = True
    elif (
        ("Anything else for " in message or "Anything else from " in message)
        and "1. Yes" in message
    ):
        rows = [["Yes", "No"]]
        one_time = True
    elif any(
        marker in message
        for marker in (
            "Choose a food to edit:",
            "Choose a food to save as a favorite:",
            "Choose a favorite to log:",
            "Choose a favorite to remove:",
            "Choose a saved food to view:",
            "Choose a saved food to edit:",
            "Choose a saved food to delete:",
            "Choose an entered food to view:",
            "Choose an entered food to edit:",
            "Choose an entered food to remove:",
            "Choose a saved recipe to view:",
            "Choose a saved recipe to edit:",
            "Choose a saved recipe to delete:",
            "Choose a Pantry item to remove:",
            "Choose a meal from yesterday to copy:",
        )
    ):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index:index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.append(["Back", "Cancel"])
    elif "What would you like to change?" in message:
        rows = [
            ["Quantity", "Meal"],
            ["Back", "Cancel"],
        ]
    elif "Which meal was this?" in message:
        choices = re.findall(
            r"(?m)^\d+\.\s+(.+?)\s*$",
            message,
        )
        rows = [
            choices[index:index + 2]
            for index in range(0, len(choices), 2)
        ]
        rows.append(["Cancel"])
        one_time = True
    elif "Which meal was this for?" in message:
        rows = [
            ["Before breakfast", "Breakfast"],
            ["Morning snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Cancel"],
        ]
        one_time = True
    elif "Choose the new meal:" in message:
        rows = [
            ["Before breakfast", "Breakfast"],
            ["School snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Back", "Cancel"],
        ]
    elif "Apply this food change?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif any(
        marker in message
        for marker in (
            "Save this favorite?",
            "Quick-log this favorite?",
            "Remove this favorite?",
            "Save this saved food?",
            "Save these nutrition changes?",
            "Save this Saved Food change?",
            "Remove this Saved Food?",
            "Add this food to My Pantry?",
            "Add this personal search name?",
            "Save this Food Library name change?",
            "Archive this entered food?",
            "Consolidate these Food Library records?",
            "Save this Pantry organization?",
            "Remove this item from My Pantry?",
            "Add these items to My Pantry?",
            "Remove this Pantry item?",
            "Delete this Pantry item from My Pantry?",
            "Clear My Pantry?",
            "Add these items to the Shopping List?",
            "Mark this Shopping List item purchased?",
            "Remove this Shopping List item?",
            "Clear the Shopping List?",
            "Save this recipe?",
            "Save this recipe change?",
            "Save these recipe nutrition changes?",
            "Delete this Saved Recipe?",
            "Copy yesterday's food?",
            "Copy this meal from yesterday?",
            "Add the new Saved Food ingredients created during this "
            "import to My Pantry?",
            "Use this Saved Food for this imported ingredient?",
        )
    ):
        rows = [["Yes", "No"]]
        one_time = True
    elif "Restaurant Recommendations\n\n" in message:
        rows = [
            ["Search again", "Back"],
            ["Cancel"],
        ]
    elif "Entered Food Details\n\n" in message:
        rows = [
            ["Edit Entered Food", "Remove Entered Food"],
            ["Back", "Cancel"],
        ]
    elif "Food Library Menu\n\n" in message:
        rows = [
            ["Find a food", "Browse entered foods"],
            ["Add entered food", "Edit entered food"],
            ["Remove entered food"],
            ["Review possible duplicates"],
            ["Library health check"],
            ["Back", "Cancel"],
        ]
    elif "Food Library Health Check — Item\n\n" in message:
        rows = [["Manage Food"], ["Back", "Cancel"]]
    elif message.startswith("Food Library Health Check — "):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index:index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.extend([["Previous", "Next"], ["Back", "Cancel"]])
    elif "Food Library Health Check\n\n" in message:
        rows = [
            ["Complete Pantry nutrition"],
            ["Organize Pantry"],
            ["Review nutrition gaps"],
            ["Review sources", "Review source rechecks"],
            ["Back", "Cancel"],
        ]
    elif "Food Library Cleanup — Possible Duplicates\n\n" in message:
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index:index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.extend([["Back", "Cancel"]])
    elif "Food Library Cleanup — Compare\n\n" in message:
        rows = [
            ["Use first", "Use second"],
            ["Keep both"],
            ["Back", "Cancel"],
        ]
    elif "Food Finder — Results\n\n" in message:
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index:index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.extend([["Search again"], ["Back", "Cancel"]])
    elif "Food Finder — Details\n\n" in message:
        rows = [["Manage Food", "Search again"], ["Back", "Cancel"]]
    elif "Food Library — Manage Food\n\n" in message:
        rows = [
            ["Change name", "Pantry location and type"],
            ["Change nutrition", "Remove or archive"],
            ["Back", "Cancel"],
        ]
    elif "Food Library — Pantry Item\n\n" in message:
        rows = [
            ["Change location and type"],
            ["Remove from Pantry"],
            ["Back", "Cancel"],
        ]
    elif "Food Library — Choose Pantry Item\n\n" in message:
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [choices, ["Back", "Cancel"]]
    elif "Entered Food Edit Menu\n\n" in message:
        rows = [
            ["Name", "Serving description"],
            ["Nutrition"],
            ["Back", "Cancel"],
        ]
    elif "Saved Recipes Menu\n\n" in message:
        rows = [
            ["Browse saved recipes", "Create recipe"],
            ["Import recipe"],
            ["Edit saved recipe"],
            ["Delete saved recipe"],
            ["Back", "Cancel"],
        ]
    elif "Recipe Builder — Choose from My Pantry\n\n" in message:
        choice_numbers = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choice_numbers[index : index + 5]
            for index in range(0, len(choice_numbers), 5)
        ]
        if "Page 1 of 1" not in message:
            rows.append(["Previous", "Next"])
        rows.extend([
            ["Add new saved food"],
            ["Back", "Cancel"],
        ])
    elif message.startswith("Recipe Builder — Edit Ingredients"):
        numbered = [
            match.group(1)
            for line in message.splitlines()
            if (match := re.match(r"^(\d+)\. ", line))
        ]
        rows = [numbered[index : index + 2] for index in range(0, len(numbered), 2)]
        rows.extend([["Add ingredient"], ["Back to review", "Cancel"]])
        one_time = True
    elif message.startswith("Recipe Builder — Edit Ingredient\n\n"):
        rows = [
            ["Change amount", "Change Saved Food"],
            ["Remove ingredient"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif "Recipe Builder — Ingredients\n\n" in message:
        rows = [
            ["Add another ingredient", "Continue"],
            ["Remove last ingredient"],
            ["Cancel"],
        ]
    elif "Recipe Builder Review\n\n" in message:
        rows = [
            [
                "Save Changes"
                if "1. Save Changes" in message
                else "Save Recipe"
            ],
            ["Change ingredients", "Cancel"],
        ]
    elif "Recipe Import — Ingredient Needed" in message:
        if "Matched Saved Food:" in message:
            rows = [["Include ingredient", "Exclude ingredient"]]
        elif "I could not match this" in message:
            rows = [
                ["Try verified lookup"],
                ["Add new saved food"],
            ]
            if "You may also reply Exclude ingredient" in message:
                rows.append(["Exclude ingredient"])
        else:
            rows = [
                ["Use one Saved Food serving"],
                ["Add new saved food"],
            ]
            if "You may also reply Exclude ingredient" in message:
                rows.append(["Exclude ingredient"])
        rows.append(["Cancel"])
        one_time = True
    elif message.startswith("Recipe Import — Verified Lookup Options"):
        rows = []
        if "Try verified generic match:" in message:
            rows.append(["Try verified generic match"])
        rows.extend([
            ["Enter simpler description"],
            ["Add new saved food"],
            ["Cancel"],
        ])
        one_time = True
    elif message.startswith("Recipe Import — Verified Serving"):
        rows = [
            ["Use one verified serving"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif "Enter a simpler food description for verified lookup." in message:
        rows = [["Back", "Cancel"]]
        one_time = True
    elif "Saved Recipe Details\n\n" in message:
        rows = [
            ["Log Recipe", "Edit Recipe"],
            ["Delete Recipe"],
            ["Back", "Cancel"],
        ]
    elif "Saved Recipe Edit Menu\n\n" in message:
        rows = [
            ["Name", "Meal type"],
            ["Summary", "Ingredients"],
            ["Preparation", "Nutrition"],
            ["Back", "Cancel"],
        ]
    elif "Should this recipe be for lunch or dinner?" in message:
        rows = [["Lunch", "Dinner"], ["Back", "Cancel"]]
        one_time = True
    elif "Which meal should this recipe be logged under?" in message:
        rows = [
            ["Before breakfast", "Breakfast"],
            ["Morning snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif "How many servings of this saved recipe?" in message:
        rows = [["0.5", "1", "1.5", "2"], ["Back", "Cancel"]]
        one_time = True
    elif "Log this saved recipe?" in message:
        rows = [["Log Recipe"], ["Back", "Cancel"]]
        one_time = True
    elif "Restaurant Menu\n\n" in message:
        rows = [
            ["Find best choices online"],
            ["Back", "Cancel"],
        ]
    elif "Favorites Menu\n\n" in message:
        rows = [
            ["Quick log", "Save today's food"],
            ["Remove favorite"],
            ["Back", "Cancel"],
        ]
    elif "How many servings should be logged?" in message:
        rows = [
            ["0.5", "1", "2"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif (
        "Which meal should this barcode product be logged under?"
        in message
    ):
        rows = [
            ["Before breakfast", "Breakfast"],
            ["Morning snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif "Teach this barcode from the package label?" in message:
        rows = [
            ["Teach from label"],
            ["Try another barcode"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif "Teach This Barcode\n\n" in message:
        rows = [
            ["Yes, save it"],
            ["Retake label photo"],
            ["Cancel"],
        ]
        one_time = True
    elif "Barcode Product\n\n" in message:
        if "Selected Pantry item:" in message:
            rows = [
                ["Link nutrition"],
                ["Save Product", "Scan Another"],
                ["Back", "Cancel"],
            ]
        else:
            rows = [
                ["Add to Pantry", "Log It"],
                ["Save Product"],
                ["Scan Another"],
                ["Back", "Cancel"],
            ]
    elif "Photo Tools Menu\n\n" in message:
        rows = [
            ["Read menu photo"],
            ["Estimate meal photo"],
            ["Scan product barcode"],
            ["Back", "Cancel"],
        ]
    elif "What should I do with this photo?" in message:
        rows = [
            ["Estimate or log this meal"],
            ["Read restaurant menu"],
            ["Scan product barcode"],
            ["Add scanned product to Pantry"],
            ["Add visible items to Pantry"],
            ["Import a recipe"],
            ["Cancel"],
        ]
        one_time = True
    elif (
        "Send a clear restaurant menu photo." in message
        or "Send a clear photo of the actual meal." in message
        or "Send a clear photo of a product barcode." in message
        or "Send a clear photo of the Nutrition Facts label." in message
        or "Send a clear, straight-on photo of the Nutrition Facts" in message
        or "Send a clear photo of one Pantry shelf." in message
        or "Type the corrected Pantry item name." in message
        or "Type the new Pantry item name." in message
        or "Type the barcode number printed beneath the bars." in message
    ):
        rows = [["Back", "Cancel"]]
    elif "Food Menu\n\n" in message:
        rows = [
            ["Log food", "Log food for yesterday"],
            ["Show today"],
            ["Edit today", "Undo last"],
            ["Same as yesterday"],
            ["Favorites", "Food Library"],
            ["Saved recipes", "My Pantry"],
            ["Photo tools", "Restaurant"],
            ["Update unknown foods"],
            ["Back", "Cancel"],
        ]
    elif "Yesterday's Food Review\n\n" in message:
        rows = [
            ["Copy one meal", "Copy entire day"],
            ["Back", "Cancel"],
        ]
    elif "My Pantry Menu\n\n" in message:
        rows = [
            ["View pantry", "Add items manually"],
            ["Scan product into Pantry"],
            ["Add items from shelf photo"],
            ["Organize pantry"],
            ["Complete Pantry nutrition"],
            ["Get meal ideas", "Smart Pantry swaps"],
            ["Shopping list"],
            ["Remove pantry item", "Clear pantry"],
            ["Back", "Cancel"],
        ]
    elif message.startswith("My Pantry — Page"):
        match = re.search(r"Page (\d+) of (\d+)", message)
        rows = []
        if match:
            current_page, total_pages = map(int, match.groups())
            navigation = []
            if current_page > 1:
                navigation.append("Previous")
            if current_page < total_pages:
                navigation.append("Next")
            if navigation:
                rows.append(navigation)
        rows.append(["Back", "Cancel"])
    elif message.startswith("Pantry Organizer — Choose Item"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        match = re.search(r"Page (\d+) of (\d+)", message)
        if match:
            current_page, total_pages = map(int, match.groups())
            navigation = []
            if current_page > 1:
                navigation.append("Previous")
            if current_page < total_pages:
                navigation.append("Next")
            if navigation:
                rows.append(navigation)
        rows.append(["Search"])
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Pantry Organizer — Search Results"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        match = re.search(r"Page (\d+) of (\d+)", message)
        if match:
            current_page, total_pages = map(int, match.groups())
            navigation = []
            if current_page > 1:
                navigation.append("Previous")
            if current_page < total_pages:
                navigation.append("Next")
            if navigation:
                rows.append(navigation)
        rows.append(["Search again"])
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Pantry Organizer — Item"):
        rows = [
            ["Change name"],
            ["Change storage and food type"],
            ["View or change nutrition"],
            ["Delete Pantry item"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif message.startswith("Pantry Organizer — Nutrition"):
        if "Nutrition status: not linked" in message:
            rows = [
                ["Add or link nutrition"],
                ["Back", "Cancel"],
            ]
        else:
            rows = [
                ["Correct current values"],
                ["Replace linked nutrition"],
                ["Remove nutrition link"],
                ["Back", "Cancel"],
            ]
        one_time = True
    elif message.startswith("Complete Pantry Nutrition — Choose Item"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        match = re.search(r"Page (\d+) of (\d+)", message)
        if match:
            current_page, total_pages = map(int, match.groups())
            navigation = []
            if current_page > 1:
                navigation.append("Previous")
            if current_page < total_pages:
                navigation.append("Next")
            if navigation:
                rows.append(navigation)
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Complete Pantry Nutrition — Options"):
        rows = [
            ["Link existing nutrition"],
            ["Try verified lookup"],
            ["Scan barcode", "Photograph label"],
            ["Enter manually"],
        ]
        if "6. Skip" in message:
            rows.append(["Skip"])
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Complete Pantry Nutrition — Existing Nutrition"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        match = re.search(r"Page (\d+) of (\d+)", message)
        if match:
            current_page, total_pages = map(int, match.groups())
            navigation = []
            if current_page > 1:
                navigation.append("Previous")
            if current_page < total_pages:
                navigation.append("Next")
            if navigation:
                rows.append(navigation)
        rows.append(["Back", "Cancel"])
        one_time = True
    elif (
        message.startswith("Link this nutrition to the Pantry item?")
        or message.startswith("Use this verified nutrition for the Pantry item?")
        or message.startswith("Use this Nutrition Facts label for the Pantry item?")
    ):
        rows = [["Yes", "No"], ["Cancel"]]
        one_time = True
    elif message.startswith("Pantry Organizer — Storage Area"):
        labels = list(PANTRY_STORAGE_AREAS.values())
        rows = [labels[index : index + 2] for index in range(0, len(labels), 2)]
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Pantry Organizer — Food Type"):
        labels = list(PANTRY_FOOD_CATEGORIES.values())
        rows = [labels[index : index + 2] for index in range(0, len(labels), 2)]
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Save this Pantry organization?"):
        rows = [["Yes", "No"], ["Cancel"]]
        one_time = True
    elif message.startswith("Rename this Pantry item?"):
        rows = [["Yes", "No"], ["Cancel"]]
        one_time = True
    elif message.startswith("Choose a Pantry item to remove:"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        match = re.search(r"Page (\d+) of (\d+)", message)
        if match:
            current_page, total_pages = map(int, match.groups())
            navigation = []
            if current_page > 1:
                navigation.append("Previous")
            if current_page < total_pages:
                navigation.append("Next")
            if navigation:
                rows.append(navigation)
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Pantry Shelf Photo — Items Found"):
        rows = [
            ["Add another photo", "Review items"],
            ["Cancel"],
        ]
        one_time = True
    elif message.startswith("Pantry Shelf Photo — Review"):
        rows = [
            ["Add all to Pantry"],
            ["Edit item name", "Remove an item"],
            ["Add another photo"],
            ["Cancel"],
        ]
        one_time = True
    elif message.startswith("Pantry Shelf Photo — Edit Item"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.append(["Back", "Cancel"])
        one_time = True
    elif message.startswith("Pantry Shelf Photo — Remove Item"):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index : index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.append(["Back", "Cancel"])
        one_time = True
    elif "Smart Pantry Swaps\n\n" in message:
        choices = re.findall(r"(?m)^(\d+)\. Replace:", message)
        add_choices = [f"Add {choice}" for choice in choices]
        rows = []
        if add_choices:
            rows.append(add_choices)
        rows.extend(
            [
                ["Shopping list", "Refresh swaps"],
                ["Back", "Cancel"],
            ]
        )
    elif "Shopping List Menu\n\n" in message:
        rows = [
            ["View list", "Add items manually"],
            ["Mark purchased", "Remove item"],
            ["Clear list"],
            ["Back", "Cancel"],
        ]
    elif (
        "Choose a Shopping List item to mark purchased:" in message
        or "Choose a Shopping List item to remove:" in message
    ):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [choices, ["Back", "Cancel"]]
    elif "Shopping List\n\n" in message:
        rows = [["Back", "Cancel"]]
    elif "Pantry Meal Ideas —" in message:
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [choices, ["More ideas"], ["Back", "Cancel"]]
    elif "Pantry Meal Idea\n\n" in message:
        rows = [
            ["Log Meal", "Save Recipe"],
            ["More ideas"],
            ["Back", "Cancel"],
        ]
    elif "What meal do you want Pantry ideas for?" in message:
        rows = [["Lunch", "Dinner"], ["Back", "Cancel"]]
        one_time = True
    elif message.startswith(
        "Can these meal ideas include foods that are not currently"
    ):
        rows = [
            ["Pantry only", "Up to 2 extra ingredients"],
            ["Back", "Cancel"],
        ]
        one_time = True
    elif "How many servings of this Pantry meal did you eat?" in message:
        rows = [["0.5", "1", "1.5", "2"], ["Back", "Cancel"]]
        one_time = True
    elif "Log this Pantry meal estimate?" in message:
        rows = [["Log Meal"], ["Back", "Cancel"]]
        one_time = True
    elif "My Pantry\n\n" in message:
        rows = [["Back", "Cancel"]]
    elif "Health Menu\n\n" in message:
        rows = [
            ["Current status", "Record sleep"],
            ["Record weight", "Health history"],
            ["Daily health insight", "Weekly health insight"],
            ["Back", "Cancel"],
        ]
    elif message.startswith("Daily Health Insight"):
        rows = [
            ["Refresh insight"],
            ["Back", "Cancel"],
        ]
    elif message.startswith("Weekly Health Insight"):
        rows = [
            ["Refresh weekly insight"],
            ["Back", "Cancel"],
        ]
    elif message.startswith("Health History"):
        rows = [
            ["7 days", "14 days", "30 days"],
            ["Back", "Cancel"],
        ]
    elif message.startswith("Heart Health Report"):
        rows = [
            ["7 days", "14 days", "30 days"],
            ["Back", "Cancel"],
        ]
    elif "Reports Menu\n\n" in message:
        rows = [
            ["Today", "Weekly report"],
            ["Goals"],
            ["Heart health"],
            ["Back", "Cancel"],
        ]
    elif "Goals Menu\n\n" in message:
        rows = [
            ["View active goal", "Add weight goal"],
            ["Update goal", "Edit goal"],
            ["Remove goal", "Goal history"],
            ["Back", "Cancel"],
        ]
    elif "Save this weight goal?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif "Save these goal changes?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif "Remove this active weight goal?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif "HealthCoach Menu\n\n" in message:
        rows = [
            ["Food", "Health"],
            ["Reports", "Help"],
            ["Cancel"],
        ]
    else:
        return None

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": one_time,
    }


def send_telegram_msg(
    message,
    chat_id=None,
    *,
    remove_keyboard=False,
):
    target_chat_id = str(chat_id or CHAT_ID) if (chat_id or CHAT_ID) else None

    if not TELEGRAM_TOKEN or not target_chat_id:
        logging.error("Missing Telegram credentials")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message}

    if remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    else:
        reply_markup = menu_reply_markup(message)
        if reply_markup:
            payload["reply_markup"] = reply_markup

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            logging.error("Telegram rejected: %s", r.text)
            return False

        response_data = r.json()
        result = response_data.get("result") or {}

        return result.get("message_id") or True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


def get_gspread_client():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_PATH, SCOPE)
    return gspread.authorize(creds)


def get_sheet_for_date(target_date):
    client = get_gspread_client()
    spreadsheet = client.open("Health Tracker")
    month = target_date.strftime("%B %Y")

    try:
        worksheet = spreadsheet.worksheet(month)
        ensure_health_tracker_schema(worksheet)
        return worksheet
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=month,
            rows="500",
            cols=str(len(HEADERS)),
        )
        ws.append_row(HEADERS)
        return ws


def ensure_health_tracker_schema(sheet):
    """Append newly supported tracker columns without moving old data."""
    current_columns = int(getattr(sheet, "col_count", 0) or 0)
    missing_columns = len(HEADERS) - current_columns

    if missing_columns <= 0:
        return False

    sheet.add_cols(missing_columns)
    for column_index in range(current_columns, len(HEADERS)):
        sheet.update_cell(
            1,
            column_index + 1,
            HEADERS[column_index],
        )
    logging.info(
        "Extended Health Tracker sheet to %s columns",
        len(HEADERS),
    )
    return True


def get_current_sheet():
    return get_sheet_for_date(datetime.now(PACIFIC_TZ).date())


def safe_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def parse_walking_heart_rate(value):
    """Return one plausible bpm value or None for malformed input."""
    parsed = safe_float(value, None)
    if parsed is None or not 0 < parsed <= MAX_WALKING_HEART_RATE_BPM:
        return None
    return parsed


def parse_positive_float(value):
    """Return a positive numeric measurement or None."""
    parsed = safe_float(value, None)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def parse_health_measurement_timestamp(value):
    """Return an Apple Health measurement time in Pacific time."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = (
            str(value or "")
            .replace("\u202f", " ")
            .replace("\u00a0", " ")
            .strip()
        )
        if not text:
            return None
        if len(text.splitlines()) != 1:
            return None

        parsed = None
        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            )
        except ValueError:
            for date_format in (
                "%m/%d/%Y %I:%M %p",
                "%m/%d/%Y, %I:%M %p",
                "%m/%d/%y %I:%M %p",
                "%m/%d/%y, %I:%M %p",
                "%b %d, %Y at %I:%M %p",
                "%B %d, %Y at %I:%M %p",
                "%Y-%m-%d %H:%M:%S",
            ):
                try:
                    parsed = datetime.strptime(text, date_format)
                    break
                except ValueError:
                    continue

        if parsed is None:
            return None

    if parsed.tzinfo is None:
        return PACIFIC_TZ.localize(parsed)
    return parsed.astimezone(PACIFIC_TZ)


def parse_timestamped_health_metric(
    value,
    measured_at_value,
    *,
    expected_date=None,
    value_parser=None,
):
    """Return one metric and its optional verified Apple source time.

    A missing timestamp keeps legacy Shortcut payloads and Tracker rows
    readable. Once a timestamp is supplied, it must parse and match the
    Tracker day so an older Apple sample cannot become a new daily reading.
    """
    parser = value_parser or (lambda raw: safe_float(raw, None))
    parsed_value = parser(value)
    if parsed_value is None:
        return None, None

    if measured_at_value in ("", None):
        return parsed_value, None

    measured_at = parse_health_measurement_timestamp(
        measured_at_value
    )
    if measured_at is None:
        return None, None
    if (
        expected_date is not None
        and measured_at.date() != expected_date
    ):
        return None, None
    return parsed_value, measured_at


def parse_blood_pressure(
    systolic_value,
    diastolic_value,
    measured_at_value,
    *,
    expected_date=None,
):
    """Validate one paired blood-pressure reading and its source time."""
    systolic = safe_float(systolic_value, None)
    diastolic = safe_float(diastolic_value, None)
    measured_at = parse_health_measurement_timestamp(
        measured_at_value
    )

    if (
        systolic is None
        or diastolic is None
        or measured_at is None
        or not 0 < systolic <= MAX_BLOOD_PRESSURE_MMHG
        or not 0 < diastolic <= MAX_BLOOD_PRESSURE_MMHG
    ):
        return None

    if expected_date is not None and measured_at.date() != expected_date:
        return None

    return {
        "systolic": systolic,
        "diastolic": diastolic,
        "measured_at": measured_at,
    }


def safe_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def parse_sleep(value):
    if value in ("", None):
        return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if ":" in value:
            try:
                h, m = value.split(":")
                return float(h) + float(m) / 60
            except Exception:
                return None

    try:
        return float(value)
    except Exception:
        return None


def normalize_sleep_for_sheet(value):
    if value in ("", None):
        return ""
    if isinstance(value, str) and ":" in value:
        return value.strip()
    parsed = parse_sleep(value)
    return parsed if parsed is not None else ""


def format_sleep_for_humans(raw_value):
    if raw_value in ("", None):
        return "not recorded"

    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return "not recorded"
        if ":" in stripped:
            parsed = parse_sleep(stripped)
            if parsed is not None:
                return f"{stripped} ({parsed:.2f} hrs)"
            return stripped
        parsed = parse_sleep(stripped)
        if parsed is not None:
            return f"{parsed:.2f} hrs"
        return stripped

    parsed = parse_sleep(raw_value)
    if parsed is None:
        return "not recorded"
    return f"{parsed:.2f} hrs"


def parse_row_date(row):
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], "%m/%d/%Y %I:%M %p").date()
    except Exception:
        return None


def parse_timestamp(timestamp_str):
    if not timestamp_str:
        return None
    try:
        dt = datetime.strptime(timestamp_str, "%m/%d/%Y %I:%M %p")
        return PACIFIC_TZ.localize(dt)
    except Exception:
        return None


def row_to_metrics(row):
    if not row:
        return None

    padded = list(row) + [""] * (len(HEADERS) - len(row))

    weight_val = safe_float(padded[6], None)
    if weight_val == 0:
        weight_val = None

    row_date = parse_row_date(padded)
    rhr_value, rhr_measured_at = parse_timestamped_health_metric(
        padded[5],
        padded[16],
        expected_date=row_date,
    )
    hrv_value, hrv_measured_at = parse_timestamped_health_metric(
        padded[7],
        padded[17],
        expected_date=row_date,
        value_parser=parse_positive_float,
    )
    cardio_fitness, cardio_fitness_measured_at = (
        parse_timestamped_health_metric(
            padded[11],
            padded[18],
            expected_date=row_date,
        )
    )
    walking_heart_rate, walking_heart_rate_measured_at = (
        parse_timestamped_health_metric(
            padded[12],
            padded[19],
            expected_date=row_date,
            value_parser=parse_walking_heart_rate,
        )
    )
    blood_pressure = parse_blood_pressure(
        padded[13],
        padded[14],
        padded[15],
        expected_date=row_date,
    )

    return {
        "timestamp": padded[0],
        "steps": safe_int(padded[1], 0),
        "total_cals": safe_float(padded[2], 0),
        "active_cals": safe_float(padded[3], 0),
        "sleep_hours": parse_sleep(padded[4]),
        "sleep_raw": padded[4],
        "rhr": rhr_value,
        "rhr_measured_at": rhr_measured_at,
        "weight": weight_val,
        "hrv": hrv_value,
        "hrv_measured_at": hrv_measured_at,
        "dietary_cals": safe_float(padded[8], 0),
        "protein": safe_float(padded[9], 0),
        "exercise_minutes": safe_float(padded[10], None),
        "cardio_fitness": cardio_fitness,
        "cardio_fitness_measured_at": cardio_fitness_measured_at,
        "walking_heart_rate_average": walking_heart_rate,
        "walking_heart_rate_measured_at": (
            walking_heart_rate_measured_at
        ),
        "blood_pressure_systolic": (
            blood_pressure["systolic"]
            if blood_pressure is not None
            else None
        ),
        "blood_pressure_diastolic": (
            blood_pressure["diastolic"]
            if blood_pressure is not None
            else None
        ),
        "blood_pressure_measured_at": (
            blood_pressure["measured_at"]
            if blood_pressure is not None
            else None
        ),
    }


def get_row_for_date(target_date):
    sheet = get_sheet_for_date(target_date)
    rows = sheet.get_all_values()

    target_str = target_date.strftime("%m/%d/%Y")
    match = None

    for row in rows[1:]:
        if row and row[0].startswith(target_str):
            match = row

    return match


def get_today_row_index_and_row(sheet, today_str):
    rows = sheet.get_all_values()
    match_index = None
    match_row = None

    for i, row in enumerate(rows[1:], start=2):
        if row and row[0].startswith(today_str):
            match_index = i
            match_row = row

    if match_row:
        while len(match_row) < len(HEADERS):
            match_row.append("")

    return match_index, match_row, rows


def get_recent_rows(reference_date, days_back=10, exclude_dates=None):
    exclude_dates = exclude_dates or set()
    rows = []

    seen_months = {}
    for i in range(days_back + 1):
        d = reference_date - timedelta(days=i)
        key = d.strftime("%Y-%m")
        seen_months[key] = d

    for sample_date in seen_months.values():
        try:
            sheet = get_sheet_for_date(sample_date)
            rows.extend(sheet.get_all_values()[1:])
        except Exception as e:
            logging.error("Recent rows read error: %s", e)

    filtered = []
    min_date = reference_date - timedelta(days=days_back)

    for row in rows:
        row_date = parse_row_date(row)
        if not row_date:
            continue
        if row_date < min_date or row_date > reference_date:
            continue
        if row_date in exclude_dates:
            continue
        filtered.append(row)

    filtered.sort(key=lambda r: parse_row_date(r))
    return filtered


def collect_recent_numeric_values(rows, col_index, limit=7, minimum_valid=None, parser=None):
    parser = parser or (lambda x: safe_float(x, None))
    values = []

    for row in reversed(rows):
        if len(row) <= col_index:
            continue
        raw = row[col_index]
        if raw in ("", None):
            continue

        val = parser(raw)
        if val is None:
            continue

        if minimum_valid is not None and val <= minimum_valid:
            continue

        values.append(val)
        if len(values) >= limit:
            break

    values.reverse()
    return values


def average_or_default(values, default=None):
    return sum(values) / len(values) if values else default


def get_recent_average_weight(reference_date, days_back=10, limit=7):
    recent_rows = get_recent_rows(
        reference_date=reference_date,
        days_back=days_back,
        exclude_dates={reference_date},
    )
    weights = collect_recent_numeric_values(
        recent_rows,
        6,
        limit=limit,
        minimum_valid=50,
    )
    return average_or_default(weights, None)


def update_or_insert_today(
    sheet,
    row,
    now,
    *,
    clear_existing_columns=(),
):
    today_str = now.strftime("%m/%d/%Y")
    row_index, existing_row, _ = get_today_row_index_and_row(sheet, today_str)

    if row_index and existing_row:
        incoming = list(row) + [""] * (len(HEADERS) - len(row))
        existing = list(existing_row) + [""] * (
            len(HEADERS) - len(existing_row)
        )
        merged = [incoming[0]]
        merged.extend(
            incoming[index]
            if incoming[index] not in ("", None)
            else existing[index]
            for index in range(1, len(HEADERS))
        )
        # A legacy value without a source time must not inherit a timestamp
        # from a different measurement already stored in today's row.
        for value_index, measured_at_index in (
            (5, 16),
            (7, 17),
            (11, 18),
            (12, 19),
        ):
            if incoming[value_index] not in ("", None):
                merged[measured_at_index] = incoming[measured_at_index]
        for index in clear_existing_columns:
            if 0 < index < len(merged):
                merged[index] = ""
        sheet.update(
            range_name=(
                f"A{row_index}:{TRACKER_LAST_COLUMN}{row_index}"
            ),
            values=[merged],
        )
        logging.info("Updated row %s for %s", row_index, today_str)
    else:
        sheet.append_row(row)
        logging.info("Appended new row for %s", today_str)


def sync_food_ledger_totals_to_sheet(target_date):
    """
    Recalculate one day's nutrition from the Food Ledger and
    overwrite only Dietary Cals and Protein in the Health Tracker.
    """
    totals = get_daily_totals(target_date)

    dietary_cals = round(
        float(totals.get("calories") or 0),
        3,
    )
    protein = round(
        float(totals.get("protein_g") or 0),
        3,
    )

    sheet = get_sheet_for_date(target_date)
    date_str = target_date.strftime("%m/%d/%Y")

    row_index, existing_row, _ = (
        get_today_row_index_and_row(
            sheet,
            date_str,
        )
    )

    if row_index and existing_row:
        sheet.update(
            range_name=f"I{row_index}:J{row_index}",
            values=[[dietary_cals, protein]],
        )

        logging.info(
            "Synced Food Ledger nutrition to row %s for %s: "
            "calories=%s protein=%s",
            row_index,
            date_str,
            dietary_cals,
            protein,
        )

    else:
        now = datetime.now(PACIFIC_TZ)

        timestamp = (
            now.strftime("%m/%d/%Y %I:%M %p")
            if now.date() == target_date
            else target_date.strftime("%m/%d/%Y 12:00 AM")
        )

        row = [
            timestamp,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            dietary_cals,
            protein,
            "",
        ]

        sheet.append_row(row)

        logging.info(
            "Created Health Tracker row from Food Ledger for %s: "
            "calories=%s protein=%s",
            date_str,
            dietary_cals,
            protein,
        )

    return {
        "entry_date": target_date.isoformat(),
        "dietary_cals": dietary_cals,
        "protein": protein,
    }


def build_progress_message(label, metrics):
    if not metrics:
        return f"{label}\nNo data recorded yet today."

    sleep_text = "not recorded"
    if metrics["sleep_hours"] is not None:
        sleep_text = format_sleep_for_humans(metrics.get("sleep_raw"))

    weight_text = "not recorded"
    if metrics["weight"] is not None:
        weight_text = f"{metrics['weight']:.1f} lbs"

    exercise_minutes = metrics.get("exercise_minutes")
    exercise_text = "not recorded"
    if exercise_minutes is not None:
        exercise_text = (
            f"{format_display_number(exercise_minutes)} min"
        )

    resting_heart_rate = metrics.get("rhr")
    resting_heart_rate_text = "not recorded"
    if resting_heart_rate is not None:
        resting_heart_rate_text = (
            f"{format_display_number(resting_heart_rate)} bpm"
        )
        if metrics.get("rhr_measured_at") is not None:
            resting_heart_rate_text += (
                " at "
                + metrics["rhr_measured_at"]
                .strftime("%I:%M %p")
                .lstrip("0")
            )

    hrv = metrics.get("hrv")
    hrv_text = "not recorded"
    if hrv is not None:
        hrv_text = f"{format_display_number(hrv)} ms"
        if metrics.get("hrv_measured_at") is not None:
            hrv_text += (
                " at "
                + metrics["hrv_measured_at"]
                .strftime("%I:%M %p")
                .lstrip("0")
            )

    cardio_fitness = metrics.get("cardio_fitness")
    cardio_fitness_text = "not recorded"
    if cardio_fitness is not None:
        cardio_fitness_text = (
            f"{format_display_number(cardio_fitness)} mL/kg/min"
        )
        if metrics.get("cardio_fitness_measured_at") is not None:
            cardio_fitness_text += (
                " at "
                + metrics["cardio_fitness_measured_at"]
                .strftime("%I:%M %p")
                .lstrip("0")
            )

    walking_heart_rate = metrics.get(
        "walking_heart_rate_average"
    )
    walking_heart_rate_text = "not recorded"
    if walking_heart_rate is not None:
        walking_heart_rate_text = (
            f"{format_display_number(walking_heart_rate)} bpm"
        )
        if (
            metrics.get("walking_heart_rate_measured_at")
            is not None
        ):
            walking_heart_rate_text += (
                " at "
                + metrics["walking_heart_rate_measured_at"]
                .strftime("%I:%M %p")
                .lstrip("0")
            )

    blood_pressure_text = "not recorded"
    blood_pressure_systolic = metrics.get(
        "blood_pressure_systolic"
    )
    blood_pressure_diastolic = metrics.get(
        "blood_pressure_diastolic"
    )
    blood_pressure_measured_at = metrics.get(
        "blood_pressure_measured_at"
    )
    if (
        blood_pressure_systolic is not None
        and blood_pressure_diastolic is not None
        and blood_pressure_measured_at is not None
    ):
        blood_pressure_text = (
            f"{format_display_number(blood_pressure_systolic)}/"
            f"{format_display_number(blood_pressure_diastolic)} "
            "mmHg at "
            f"{blood_pressure_measured_at.strftime('%I:%M %p').lstrip('0')}"
        )

    return (
        f"{label}\n"
        f"Steps: {metrics['steps']}\n"
        f"Total burn: {metrics['total_cals']:.0f}\n"
        f"Calories consumed: {metrics['dietary_cals']:.0f}\n"
        f"Protein: {metrics['protein']:.0f}g\n"
        f"Exercise: {exercise_text}\n"
        f"Resting heart rate: {resting_heart_rate_text}\n"
        f"HRV: {hrv_text}\n"
        f"Cardio fitness: {cardio_fitness_text}\n"
        f"Walking heart rate: {walking_heart_rate_text}\n"
        f"Blood pressure: {blood_pressure_text}\n"
        f"Sleep: {sleep_text}\n"
        f"Weight: {weight_text}"
    )


def get_today_metrics():
    today_row = get_row_for_date(datetime.now(PACIFIC_TZ).date())
    return row_to_metrics(today_row)


def get_today_sleep_raw():
    metrics = get_today_metrics()
    if not metrics:
        return ""
    return metrics.get("sleep_raw", "")


def sleep_is_recorded_for_today():
    raw_sleep = get_today_sleep_raw()
    return raw_sleep not in ("", None)


def set_today_sleep(sleep_text):
    normalized_sleep = normalize_sleep_for_sheet(sleep_text)
    if normalized_sleep == "":
        return False, "I couldn't understand that sleep value. Try something like 6:22 or 6.5."

    now = datetime.now(PACIFIC_TZ)
    sheet = get_current_sheet()
    today_str = now.strftime("%m/%d/%Y")
    row_index, existing_row, _ = get_today_row_index_and_row(sheet, today_str)
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")

    if row_index and existing_row:
        while len(existing_row) < 10:
            existing_row.append("")
        merged = list(existing_row[:10])
        merged[0] = timestamp
        merged[4] = normalized_sleep
        sheet.update(
            range_name=f"A{row_index}:J{row_index}",
            values=[merged],
        )
        logging.info("Updated sleep in row %s for %s", row_index, today_str)
    else:
        row = [
            timestamp,
            "",
            "",
            "",
            normalized_sleep,
            "",
            "",
            "",
            "",
            "",
        ]
        sheet.append_row(row)
        logging.info("Appended new row with sleep for %s", today_str)

    return (
        True,
        "Recorded sleep as "
        f"{format_sleep_for_humans(normalized_sleep)} for today.",
    )


def set_today_weight(weight):
    """Save one official weight for today; later entries correct it."""
    try:
        normalized_weight = float(weight)
    except (TypeError, ValueError):
        return False, "I couldn't understand that weight."

    if not 50 <= normalized_weight <= 700:
        return (
            False,
            "That weight seems outside the expected range. "
            "Please enter pounds between 50 and 700.",
        )

    now = datetime.now(PACIFIC_TZ)
    sheet = get_current_sheet()
    today_str = now.strftime("%m/%d/%Y")
    row_index, existing_row, _ = get_today_row_index_and_row(
        sheet,
        today_str,
    )
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")
    replacing = False

    if row_index and existing_row:
        while len(existing_row) < 10:
            existing_row.append("")

        replacing = existing_row[6] not in ("", None)
        merged = list(existing_row[:10])
        merged[0] = timestamp
        merged[6] = normalized_weight

        sheet.update(
            range_name=f"A{row_index}:J{row_index}",
            values=[merged],
        )
    else:
        row = [
            timestamp,
            "",
            "",
            "",
            "",
            "",
            normalized_weight,
            "",
            "",
            "",
        ]
        sheet.append_row(row)

    action = "Corrected" if replacing else "Recorded"
    return (
        True,
        f"{action} today's official weight to "
        f"{normalized_weight:.1f} lbs.",
    )


def answer_sleep_status():
    raw_sleep = get_today_sleep_raw()
    if raw_sleep in ("", None):
        return 'No, sleep is not recorded for today. Reply with "Record my sleep as 6:22".'
    return f"Yes, sleep is recorded for today: {format_sleep_for_humans(raw_sleep)}."


def extract_sleep_value_from_text(text, *, allow_bare=False):
    """Extract flexible sleep durations and return decimal hours."""
    lowered = text.lower().strip()

    if allow_bare:
        bare_match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+|:[0-9]{1,2})?)",
            lowered,
        )
        if bare_match:
            value = parse_sleep(bare_match.group(1))
            if value is not None and 0 < value <= 24:
                return value

    range_match = re.search(
        r"(?:slept\s+)?from\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+"
        r"(?:to|until|till)\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        lowered,
    )

    if range_match:
        start_hour = int(range_match.group(1))
        start_minute = int(range_match.group(2) or 0)
        start_period = range_match.group(3)
        end_hour = int(range_match.group(4))
        end_minute = int(range_match.group(5) or 0)
        end_period = range_match.group(6)

        def minutes_after_midnight(hour, minute, period):
            if minute >= 60 or hour > 12:
                return None
            if period == "am":
                hour = 0 if hour == 12 else hour
            elif period == "pm":
                hour = 12 if hour == 12 else hour + 12
            return hour * 60 + minute

        start = minutes_after_midnight(
            start_hour,
            start_minute,
            start_period,
        )
        end = minutes_after_midnight(
            end_hour,
            end_minute,
            end_period,
        )

        if start is not None and end is not None:
            if start_period is None and end_period is None:
                if start_hour <= 12 and end_hour <= 12:
                    # Assume a typical overnight range.
                    if start_hour < 7:
                        start += 12 * 60

            duration = end - start
            if duration <= 0:
                duration += 24 * 60

            hours = duration / 60
            if 0 < hours <= 24:
                return hours

    half_match = re.search(
        r"(?:i\s+)?(?:slept|got|had)\s+"
        r"(\d+)\s+and\s+a\s+half\s+hours?",
        lowered,
    )
    if half_match:
        return float(half_match.group(1)) + 0.5

    hours_minutes_match = re.search(
        r"(?:i\s+)?(?:slept|got|had)\s+"
        r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)"
        r"(?:\s+(?:and\s+)?(\d+)\s*(?:minutes?|mins?))?",
        lowered,
    )
    if hours_minutes_match:
        hours = float(hours_minutes_match.group(1))
        minutes = float(hours_minutes_match.group(2) or 0)
        value = hours + minutes / 60
        if 0 < value <= 24 and minutes < 60:
            return value

    match = re.search(
        r"(?:record\s+my\s+sleep\s+as|"
        r"record\s+sleep\s+as|"
        r"sleep\s+(?:was|is))\s+"
        r"([0-9]+(?:\.[0-9]+|:[0-9]{1,2})?)",
        lowered,
    )

    if match:
        value = parse_sleep(match.group(1))
        if value is not None and 0 < value <= 24:
            return value

    return None


def extract_weight_value_from_text(text, *, allow_bare=False):
    """Extract a plausible weight in pounds from natural language."""
    lowered = text.lower().strip()

    patterns = [
        r"(?:record\s+my\s+weight\s+as|"
        r"record\s+weight\s+as|"
        r"i\s+weighed|"
        r"i\s+weigh|"
        r"my\s+weight\s+(?:was|is))\s+"
        r"(\d{2,3}(?:\.\d+)?)",
    ]

    if allow_bare:
        patterns.append(r"(\d{2,3}(?:\.\d+)?)")

    for pattern in patterns:
        match = re.fullmatch(pattern, lowered) or re.search(
            pattern,
            lowered,
        )
        if not match:
            continue

        value = float(match.group(1))

        if 50 <= value <= 700:
            return value

    return None


def is_sleep_status_question(text):
    lowered = text.lower().strip()

    patterns = [
        r"did\s+i\s+record\s+my\s+sleep(?:\s+today)?\??",
        r"did\s+i\s+record\s+my\s+sleep\s+for\s+today\??",
        r"have\s+i\s+recorded\s+my\s+sleep(?:\s+today)?\??",
        r"is\s+my\s+sleep\s+recorded(?:\s+today)?\??",
        r"did\s+you\s+record\s+my\s+sleep(?:\s+today)?\??",
        r"what\s+is\s+my\s+sleep(?:\s+today)?\??",
    ]

    return any(re.search(pattern, lowered) for pattern in patterns)


def is_run_weekly_report_command(text):
    lowered = text.lower().strip()
    return lowered in {
        "run weekly report",
        "/runweeklyreport",
        "run the weekly report",
    }


def get_time_aware_meal_options():
    """Return meal choices appropriate for the current local time."""
    hour = datetime.now().astimezone().hour

    if hour < 11:
        return [
            "breakfast",
            "morning snack",
        ]

    if hour < 16:
        return [
            "morning snack",
            "lunch",
            "afternoon snack",
        ]

    return [
        "afternoon snack",
        "dinner",
        "dessert",
    ]


def parse_food_entry_date(value, *, default=None):
    """Parse a stored Food Ledger date without changing its day."""
    if isinstance(value, date):
        return value

    if value not in (None, ""):
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            logging.warning("Invalid Food entry date: %s", value)

    return default


def format_food_entry_date(entry_date) -> str:
    """Return an explicit, readable Food Ledger date label."""
    target = parse_food_entry_date(entry_date)
    if target is None:
        return ""

    today = datetime.now(PACIFIC_TZ).date()
    if target == today:
        prefix = "Today"
    elif target == today - timedelta(days=1):
        prefix = "Yesterday"
    else:
        prefix = target.strftime("%A")

    return (
        f"{prefix} — {target.strftime('%a %b')} "
        f"{target.day}, {target.year}"
    )


def extract_yesterday_food_intent(text, *, reference_date=None):
    """Detect only the immediately preceding day in natural Food text."""
    original = str(text or "").strip()
    if re.search(r"\byesterday\b", original, flags=re.IGNORECASE) is None:
        return None, original

    current = reference_date or datetime.now(PACIFIC_TZ).date()
    cleaned = re.sub(
        r"\b(?:for\s+)?yesterday(?:'s)?\b",
        " ",
        original,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return current - timedelta(days=1), cleaned


def food_logging_meal_options(entry_date=None):
    """Offer every meal for late entries; keep time-aware choices today."""
    target = parse_food_entry_date(entry_date)
    today = datetime.now(PACIFIC_TZ).date()
    if target is not None and target != today:
        return [
            "before breakfast",
            "breakfast",
            "morning snack",
            "lunch",
            "afternoon snack",
            "dinner",
            "dessert",
        ]
    return get_time_aware_meal_options()


def prompt_for_corrected_food(
    *,
    chat_id,
    known_data,
    message="Send the corrected food description as a new message.",
):
    """Keep the selected meal and date while correcting food wording."""
    context = dict(known_data or {})
    target = parse_food_entry_date(
        context.get("_entry_date")
    )
    meal_category = str(context.get("meal_category") or "").strip().lower()
    yesterday = (
        datetime.now(PACIFIC_TZ).date()
        - timedelta(days=1)
    )

    cancel_conversation(chat_id)
    if meal_category:
        entry_date = target or datetime.now(PACIFIC_TZ).date()
        start_conversation(
            chat_id=chat_id,
            conversation_type="food_meal",
            current_step="awaiting_food",
            known_data={
                "meal_category": meal_category,
                "restaurant": context.get("restaurant"),
                "_entry_date": entry_date.isoformat(),
            },
            missing_fields=[],
            original_message="",
        )
        message += (
            f"\n\n{meal_category.title()} is still selected for "
            f"{format_food_entry_date(entry_date)}."
        )
    elif target == yesterday:
        start_conversation(
            chat_id=chat_id,
            conversation_type="yesterday_food_logging",
            current_step="awaiting_food",
            known_data={"_entry_date": target.isoformat()},
            missing_fields=[],
            original_message="",
        )
        message += (
            "\n\nIt will still be logged for "
            f"{format_food_entry_date(target)}."
        )

    send_telegram_msg(message, chat_id=chat_id)


def format_meal_selection_prompt(interpretation, *, entry_date=None):
    """Format a focused time-aware meal question."""
    options = food_logging_meal_options(entry_date)

    lines = ["I interpreted this as:"]

    date_label = format_food_entry_date(entry_date)
    if date_label:
        lines.append(f"Date: {date_label}")

    fields = (
        ("Restaurant", interpretation.restaurant),
        ("Brand", interpretation.brand),
        ("Food", interpretation.food_name),
        ("Size", interpretation.size),
        ("Quantity", interpretation.quantity),
        ("Amount", interpretation.quantity_description),
        ("Drink", interpretation.drink),
    )

    for label, value in fields:
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    if interpretation.assumptions:
        lines.append("")
        lines.append("Assumptions:")

        for assumption in interpretation.assumptions:
            lines.append(f"- {assumption}")

    lines.append("")
    lines.append("Which meal was this?")

    for index, option in enumerate(options, start=1):
        lines.append(f"{index}. {option.title()}")

    return "\n".join(lines)


def parse_meal_selection(text, options):
    """Resolve a numbered or natural-language meal response."""
    cleaned = text.strip().lower()

    if cleaned.isdigit():
        index = int(cleaned) - 1

        if 0 <= index < len(options):
            return options[index]

    aliases = {
        "breakfast": "breakfast",
        "morning snack": "morning snack",
        "school snack": "morning snack",
        "lunch": "lunch",
        "afternoon snack": "afternoon snack",
        "dinner": "dinner",
        "dessert": "dessert",
    }

    for phrase, meal in aliases.items():
        if cleaned == phrase or phrase in cleaned:
            if meal in options:
                return meal

    return None


def format_daily_food_totals(entry_date=None):
    """Format one day's Food Ledger totals for Telegram."""
    today = datetime.now(PACIFIC_TZ).date()
    target = parse_food_entry_date(entry_date, default=today)
    totals = get_daily_totals(target)
    if target == today:
        heading = "Today's food totals:"
    elif target == today - timedelta(days=1):
        heading = (
            "Yesterday's food totals — "
            f"{target.strftime('%a %b')} {target.day}, {target.year}:"
        )
    else:
        heading = (
            f"Food totals for {target.strftime('%a %b')} "
            f"{target.day}, {target.year}:"
        )

    return "\n".join(
        [
            heading,
            "",
            (
                "Calories: "
                + format_display_number(
                    totals["calories"],
                    decimals=0,
                )
            ),
            (
                "Protein: "
                + format_display_number(
                    totals["protein_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Carbohydrates: "
                + format_display_number(
                    totals["carbohydrates_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Fat: "
                + format_display_number(
                    totals["fat_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Fiber: "
                + format_display_number(
                    totals["fiber_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Sugar: "
                + format_display_number(
                    totals["sugar_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Sodium: "
                + format_display_number(
                    totals["sodium_mg"],
                    decimals=0,
                )
                + " mg"
            ),
        ]
    )


def format_food_interpretation(interpretation, *, entry_date=None):
    """Format a food interpretation for Telegram review."""
    lines = ["I interpreted this as:"]

    date_label = format_food_entry_date(entry_date)
    if date_label:
        lines.append(f"Date: {date_label}")

    if interpretation.is_combo_meal:
        fields = (
            ("Restaurant", interpretation.restaurant),
            ("Combo", "Yes"),
            ("Entrée", interpretation.combo_entree),
            (
                "Side",
                " ".join(
                    part
                    for part in (
                        interpretation.combo_side_size,
                        interpretation.combo_side,
                    )
                    if part
                ),
            ),
            (
                "Drink",
                " ".join(
                    part
                    for part in (
                        interpretation.combo_drink_size,
                        interpretation.combo_drink,
                    )
                    if part
                ),
            ),
            ("Quantity", interpretation.quantity),
            ("Meal", interpretation.meal_category),
        )
    else:
        fields = (
            ("Restaurant", interpretation.restaurant),
            ("Brand", interpretation.brand),
            ("Food", interpretation.food_name),
            ("Size", interpretation.size),
            ("Quantity", interpretation.quantity),
            ("Amount", interpretation.quantity_description),
            ("Meal", interpretation.meal_category),
            ("Drink", interpretation.drink),
        )

    for label, value in fields:
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    if interpretation.assumptions:
        lines.append("")
        lines.append("Assumptions:")
        for assumption in interpretation.assumptions:
            lines.append(f"- {assumption}")

    if interpretation.missing_fields:
        lines.append("")
        lines.append(
            "Still needed: "
            + ", ".join(interpretation.missing_fields)
        )

    if interpretation.clarification_question:
        lines.append("")
        lines.append(interpretation.clarification_question)

    lines.append("")
    lines.append(
        "Nutrition has not been looked up or saved yet."
    )

    if not interpretation.missing_fields:
        lines.append("")
        lines.append("1. Correct")
        lines.append("2. Edit")
        lines.append("3. Cancel")

    return "\n".join(lines)


def build_help_message():
    return (
        "I can currently help with:\n"
        "- /status\n"
        '- Record sleep, like: "Record my sleep as 6:22"\n'
        '- Check sleep, like: "Did I record my sleep today?"\n'
        '- Run the weekly report, like: "run weekly report"'
    )


def build_last_week_rows(reference_date):
    end_date = reference_date - timedelta(days=1)
    start_date = end_date - timedelta(days=6)

    rows = get_recent_rows(reference_date, days_back=10)
    week = []

    for row in rows:
        row_date = parse_row_date(row)
        if not row_date:
            continue
        if start_date <= row_date <= end_date:
            metrics = row_to_metrics(row)
            if not metrics:
                continue
            week.append({
                "date": row_date.strftime("%Y-%m-%d"),
                "timestamp": metrics.get("timestamp"),
                "steps": metrics.get("steps") if metrics.get("steps") > 0 else None,
                "total_cals": metrics.get("total_cals") if metrics.get("total_cals") > 0 else None,
                "dietary_cals": metrics.get("dietary_cals") if metrics.get("dietary_cals") > 0 else None,
                "protein": metrics.get("protein") if metrics.get("protein") > 0 else None,
                "sleep_hours": metrics.get("sleep_hours"),
                "weight": metrics.get("weight"),
                "exercise_minutes": metrics.get(
                    "exercise_minutes"
                ),
            })

    week.sort(key=lambda x: x["date"])
    return week


def get_food_ledger_early_protein_for_week(reference_date):
    end_date = reference_date - timedelta(days=1)
    start_date = end_date - timedelta(days=6)

    results = {}
    current = start_date
    while current <= end_date:
        entries = list_food_entries(entry_date=current)
        early_entries = [
            entry
            for entry in entries
            if entry.get("meal_category") in EARLY_PROTEIN_MEALS
        ]
        if early_entries:
            results[current.isoformat()] = round(
                sum(
                    float(entry.get("protein_g") or 0)
                    for entry in early_entries
                ),
                1,
            )
        current += timedelta(days=1)

    return results


def evaluate_goals_for_week(week_rows, reference_date):
    goals = get_active_goals()
    lines = []
    early_protein_week = (
        get_food_ledger_early_protein_for_week(reference_date)
    )

    for goal in goals:
        goal_type = goal.get("type")
        target = goal.get("target")
        start_date = goal.get("start_date")
        weeks_active = goal.get("weeks_active", 0)

        if goal_type == "steps_daily":
            valid_days = [row for row in week_rows if row.get("steps") is not None]
            hits = sum(1 for row in valid_days if row.get("steps", 0) >= target)
            if valid_days:
                lines.append(
                    f"Steps goal ({target:,}/day): hit {hits}/{len(valid_days)} tracked days."
                )
            else:
                lines.append(
                    f"Steps goal ({target:,}/day): no tracked days available."
                )

        elif goal_type == "protein_early":
            valid_days = []
            hits = 0

            for day_key, early_protein in sorted(
                early_protein_week.items()
            ):
                valid_days.append(day_key)
                if float(early_protein) >= float(target):
                    hits += 1

            if valid_days:
                lines.append(
                    f"Protein goal ({target:.0f}g by 1 PM): hit "
                    f"{hits}/{len(valid_days)} Food Ledger days using "
                    "Breakfast + School Snack + Lunch."
                )
            else:
                lines.append(
                    f"Protein goal ({target:.0f}g by 1 PM): no "
                    "Food Ledger days with early meals available."
                )

        else:
            lines.append(f"Unknown goal type: {goal_type}")

        if start_date:
            lines.append(f"Goal start date: {start_date} | weeks active: {weeks_active}")

    return lines


def increment_goal_weeks_if_due(reference_date):
    data = load_goals()
    goals = data.get("active_goals", [])
    changed = False

    for goal in goals:
        start_date = goal.get("start_date")
        if not start_date:
            continue
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        except Exception:
            continue

        weeks_elapsed = max(0, (reference_date - start_dt).days // 7)
        if goal.get("weeks_active", 0) != weeks_elapsed:
            goal["weeks_active"] = weeks_elapsed
            changed = True

    if changed:
        save_goals(data)


def run_130_goal_check():
    now = datetime.now(PACIFIC_TZ)
    today = now.date()

    row = get_row_for_date(today)
    metrics = row_to_metrics(row)

    if not metrics:
        return send_telegram_msg("1:30 check\nNo data recorded yet today.")

    last_update = parse_timestamp(metrics.get("timestamp"))
    cutoff = now.replace(hour=13, minute=0, second=0, microsecond=0)

    if not last_update or last_update < cutoff:
        memory_enabled = True

        if MEMORY_START_DATE:
            try:
                configured_start = datetime.strptime(
                    MEMORY_START_DATE,
                    "%Y-%m-%d",
                ).date()
                memory_enabled = today >= configured_start
            except ValueError:
                logging.error(
                    "Invalid HEALTH_MEMORY_START_DATE: %s",
                    MEMORY_START_DATE,
                )

        evaluation_due = now.replace(
            hour=19,
            minute=0,
            second=0,
            microsecond=0,
        )

        if memory_enabled:
            try:
                case_result = create_case(
                    case_date=today,
                    case_type="missing_data",
                    priority="medium",
                    observation_code="food_data_missing_after_midday",
                    observation=(
                        "No food update was available after the midday check."
                    ),
                    supporting_data={
                        "check_time": now.isoformat(timespec="seconds"),
                        "last_food_update": (
                            last_update.isoformat(timespec="seconds")
                            if last_update
                            else None
                        ),
                        "dietary_cals": metrics.get("dietary_cals", 0),
                        "protein": metrics.get("protein", 0),
                    },
                    data_confidence=0.95,
                    recommendation_code="update_missing_data",
                    expected_result="Nutrition data appears later today.",
                    evaluation_due_at=evaluation_due.isoformat(
                        timespec="seconds"
                    ),
                    tags=["midday", "missing_data"],
                )
                logging.info(
                    "Missing-data Memory Case %s: case_id=%s",
                    (
                        "created"
                        if case_result["created"]
                        else "already exists"
                    ),
                    case_result["case"]["case_id"],
                )
            except Exception:
                logging.exception(
                    "Could not create missing-data Memory Case"
                )
        else:
            logging.info(
                "Memory Case collection begins on %s",
                MEMORY_START_DATE,
            )

        return send_telegram_msg(
            "1:30 check\nI do not see a food update after 1:00 PM yet. Update your spreadsheet food totals and I will check your goals."
        )

    goals = get_active_goals()
    messages = []

    for goal in goals:
        goal_type = goal.get("type")
        target = goal.get("target")

        if goal_type == "protein_early":
            protein = metrics.get("protein", 0)
            if protein >= target:
                messages.append(f"Protein goal hit: {protein:.0f}g by the latest update.")
            else:
                messages.append(f"Protein so far: {protein:.0f}g / {target}g.")

        elif goal_type == "steps_daily":
            steps = metrics.get("steps", 0)
            if steps >= target:
                messages.append(f"Steps goal already hit: {steps:,} / {target:,}.")
            else:
                messages.append(f"Steps so far: {steps:,} / {target:,}.")

    if not messages:
        messages.append("No active goals found.")

    return send_telegram_msg("1:30 check\n" + "\n".join(messages))


def send_food_coaching_for_yesterday():
    today_date = datetime.now(PACIFIC_TZ).date()
    yesterday_date = today_date - timedelta(days=1)

    yesterday_row = get_row_for_date(yesterday_date)
    yesterday = row_to_metrics(yesterday_row)

    if not yesterday:
        logging.info(
            "Skipping food coaching: no yesterday row found"
        )
        return False

    entries = list_food_entries(
        entry_date=yesterday_date
    )
    totals = get_daily_totals(yesterday_date)
    food_data = build_food_ledger_coaching_data(
        entries,
        totals,
    )

    today_row = get_row_for_date(today_date)
    today_metrics = row_to_metrics(today_row)

    recent_weight_avg = get_recent_average_weight(
        today_date
    )
    weight_today = (
        today_metrics["weight"]
        if (
            today_metrics
            and today_metrics["weight"] is not None
        )
        else yesterday["weight"]
    )
    sleep_today = (
        today_metrics["sleep_hours"]
        if today_metrics
        else None
    )

    try:
        msg = build_food_coaching(
            total_burn=yesterday["total_cals"],
            steps=yesterday["steps"],
            weight_today=weight_today,
            recent_weight_avg=recent_weight_avg,
            sleep=sleep_today,
            exercise_minutes=yesterday.get(
                "exercise_minutes"
            ),
            food_data=food_data,
        )
        return send_telegram_msg(msg)
    except Exception as error:
        logging.error(
            "Food coaching error: %s",
            error,
        )
        return False

def send_midday_update():
    today_row = get_row_for_date(datetime.now(PACIFIC_TZ).date())
    metrics = row_to_metrics(today_row)
    return send_telegram_msg(build_progress_message("1:00 update", metrics))


def send_evening_update():
    now = datetime.now(PACIFIC_TZ)
    today = now.date()

    today_row = get_row_for_date(today)
    metrics = row_to_metrics(today_row)

    latest_update = None
    nutrition_data_available = False
    dietary_cals = 0.0
    protein = 0.0

    if metrics:
        parsed_update = parse_timestamp(metrics.get("timestamp"))
        latest_update = (
            parsed_update.isoformat(timespec="seconds")
            if parsed_update
            else None
        )
        dietary_cals = metrics.get("dietary_cals", 0)
        protein = metrics.get("protein", 0)

        cutoff = now.replace(
            hour=13,
            minute=0,
            second=0,
            microsecond=0,
        )
        nutrition_data_available = bool(
            parsed_update and parsed_update >= cutoff
        )

    try:
        evaluation_results = evaluate_missing_data_cases(
            case_date=today,
            nutrition_data_available=nutrition_data_available,
            dietary_cals=dietary_cals,
            protein=protein,
            latest_update=latest_update,
        )
        logging.info(
            "Evaluated %s missing-data Memory Cases",
            len(evaluation_results),
        )
    except Exception:
        logging.exception(
            "Could not evaluate missing-data Memory Cases"
        )

    return send_telegram_msg(
        build_progress_message("7:00 update", metrics)
    )


def send_sleep_reminder_if_missing():
    if sleep_is_recorded_for_today():
        return False

    message = 'You have not recorded sleep yet today. Reply with "Record my sleep as 6:22".'
    return send_telegram_msg(message)


def send_weekly_report(chat_id=None, *, remove_keyboard=False):
    now = datetime.now(PACIFIC_TZ).date()

    initialize_goals_if_needed(now)
    increment_goal_weeks_if_due(now)

    week_rows = build_last_week_rows(now)
    base_report = build_weekly_health_report(
        week_rows,
        title="Weekly health report (previous Sunday through Saturday)",
    )
    goal_lines = evaluate_goals_for_week(week_rows, now)

    full_message = (
        f"{base_report}\n\n"
        "Goal results\n"
        + "\n".join(f"- {line}" for line in goal_lines)
    )

    return send_telegram_msg(
        full_message,
        chat_id=chat_id,
        remove_keyboard=remove_keyboard,
    )


def weekly_library_health_check_reminder_due(now, state):
    """Return whether the Sunday 9:30 AM maintenance reminder is due."""
    sunday = now.weekday() == 6
    at_or_after_930 = now.hour > 9 or (
        now.hour == 9 and now.minute >= 30
    )
    return (
        sunday
        and at_or_after_930
        and not state.get("library_health_check_reminder_sent", False)
    )


def send_library_health_check_reminder():
    """Send the weekly read-only Food Library maintenance reminder."""
    return send_telegram_msg(
        "Sunday Food Library check-in\n\n"
        "It is time to run your weekly Food Library Health Check. It "
        "reviews Pantry nutrition and organization, nutrition gaps, and "
        "source issues without changing anything automatically.\n\n"
        "Open: /menu → Food → Food Library → Library health check\n\n"
        "You can run it now or later today."
    )


def scheduler_loop():
    logging.info("Scheduler started")

    while True:
        try:
            now = datetime.now(PACIFIC_TZ)
            today_str = now.strftime("%m/%d/%Y")

            state = load_state()
            state = reset_state_for_new_day(state, today_str)

            if now.hour == 8 and now.minute == 0 and not state.get("sleep_reminder_sent", False):
                if send_sleep_reminder_if_missing():
                    state["sleep_reminder_sent"] = True
                    save_state(state)
                time.sleep(60)

            if now.hour >= 8 and now.minute >= 30 and not state.get("food_coaching_sent", False):
                if send_food_coaching_for_yesterday():
                    state["food_coaching_sent"] = True
                    save_state(state)

            if (now.hour > 13 or (now.hour == 13 and now.minute >= 0)) and not state.get("midday_sent", False):
                if send_midday_update():
                    state["midday_sent"] = True
                    save_state(state)

            if now.hour == 13 and now.minute == 30 and not state.get("goal_check_sent", False):
                if run_130_goal_check():
                    state["goal_check_sent"] = True
                    save_state(state)
                time.sleep(60)

            if (now.hour > 19 or (now.hour == 19 and now.minute >= 0)) and not state.get("evening_sent", False):
                if send_evening_update():
                    state["evening_sent"] = True
                    save_state(state)

            if now.weekday() == 6 and now.hour == 9 and now.minute == 0 and not state.get("weekly_sent", False):
                if send_weekly_report():
                    state["weekly_sent"] = True
                    save_state(state)
                time.sleep(60)

            if weekly_library_health_check_reminder_due(now, state):
                if send_library_health_check_reminder():
                    state["library_health_check_reminder_sent"] = True
                    save_state(state)
                time.sleep(60)

        except Exception as e:
            logging.error("Scheduler error: %s", e)

        time.sleep(20)


def resolve_or_create_verified_food(
    *,
    food_name: str,
    size: str | None,
    restaurant: str | None,
    brand: str | None = None,
) -> dict:
    """
    Resolve a Food Library item or retrieve and save verified nutrition.

    This function never estimates or fabricates nutrition.
    """
    serving_description = size or "standard"

    resolution = resolve_food(
        food_name=food_name,
        serving_description=serving_description,
        brand=brand,
        restaurant=restaurant,
    )

    if resolution["found"]:
        return {
            "food": resolution["food"],
            "nutrition": resolution["nutrition"],
            "source": "food_library",
            "verification_source": (
                resolution["food"].get(
                    "verification_source"
                )
            ),
        }

    provider_result = lookup_official_nutrition(
        restaurant=restaurant,
        food_name=food_name,
        size=size,
        brand=brand,
    )

    if not provider_result["found"]:
        clarification = provider_result.get(
            "clarification_question"
        )
        missing = provider_result.get("missing_fields") or []

        details = []

        if missing:
            details.append(
                "Missing: " + ", ".join(missing)
            )

        if clarification:
            details.append(clarification)

        suffix = (
            " " + " ".join(details)
            if details
            else ""
        )

        raise ValueError(
            f"Verified nutrition could not be found for "
            f"{food_name}.{suffix}"
        )

    provider_food = provider_result["food"]
    provider_nutrition = provider_result["nutrition"]
    verification = provider_result["verification"]

    saved = add_food_with_nutrition(
        canonical_name=provider_food["canonical_name"],
        serving_description=(
            provider_food["serving_description"]
        ),
        serving_amount=provider_food["serving_amount"],
        serving_unit=provider_food["serving_unit"],
        verification_status=verification["status"],
        verification_source=verification["source"],
        calories=provider_nutrition["calories"],
        protein_g=provider_nutrition["protein_g"],
        carbohydrates_g=(
            provider_nutrition["carbohydrates_g"]
        ),
        fat_g=provider_nutrition["fat_g"],
        fiber_g=provider_nutrition["fiber_g"],
        sugar_g=provider_nutrition["sugar_g"],
        sodium_mg=provider_nutrition["sodium_mg"],
        brand=provider_food["brand"],
        restaurant=provider_food["restaurant"],
        food_type=provider_food["food_type"],
        source_item_id=verification["source_item_id"],
        source_url=verification["source_url"],
    )

    return {
        "food": saved["food"],
        "nutrition": saved["nutrition"],
        "source": "verified_lookup",
        "verification_source": verification["source"],
    }


def resolve_packaged_serving_multiplier(
    *,
    food_id: int,
    quantity: float | None,
    quantity_description: str | None,
    serving_amount: float | None,
    serving_unit: str | None,
) -> float:
    """
    Convert a packaged-food amount into package servings.

    This function never estimates vague amounts.
    """
    description = (
        quantity_description or ""
    ).strip().lower()

    natural_quantity_phrases = {
        # Servings
        "a serving": "1 serving",
        "one serving": "1 serving",
        "one servings": "1 serving",
        "two servings": "2 servings",
        "three servings": "3 servings",
        "four servings": "4 servings",
        "half serving": "0.5 serving",
        "half a serving": "0.5 serving",
        "one half serving": "0.5 serving",
        "one and a half servings": "1.5 servings",
        "one and one half servings": "1.5 servings",

        # Ounces
        "an ounce": "1 oz",
        "one ounce": "1 oz",
        "two ounces": "2 oz",
        "three ounces": "3 oz",
        "four ounces": "4 oz",
        "half ounce": "0.5 oz",
        "half an ounce": "0.5 oz",
        "one half ounce": "0.5 oz",
        "one and a half ounces": "1.5 oz",
        "one and one half ounces": "1.5 oz",

        # Grams
        "one gram": "1 g",
        "two grams": "2 g",
        "three grams": "3 g",
        "four grams": "4 g",
    }

    description = natural_quantity_phrases.get(
        description,
        description,
    )

    unit_only_match = re.fullmatch(
        r"(?:servings?|g|gram|grams|grm|oz|ounce|ounces)",
        description,
    )

    if unit_only_match and quantity is not None:
        description = f"{float(quantity):g} {description}"

    if not description:
        raise ValueError(
            "A packaged-food amount is required."
        )

    serving_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*servings?",
        description,
    )

    if serving_match:
        servings = float(serving_match.group(1))

        if servings <= 0:
            raise ValueError(
                "Serving quantity must be greater than zero."
            )

        return servings

    amount_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*"
        r"(g|gram|grams|grm|oz|ounce|ounces)",
        description,
    )

    if amount_match:
        amount = float(amount_match.group(1))
        unit = amount_match.group(2)

        if amount <= 0:
            raise ValueError(
                "Food amount must be greater than zero."
            )

        if serving_amount is None or not serving_unit:
            raise ValueError(
                "The package serving size is unavailable."
            )

        normalized_serving_unit = (
            str(serving_unit).strip().lower()
        )

        fluid_units = {
            "fl oz",
            "floz",
            "fluid ounce",
            "fluid ounces",
        }

        if normalized_serving_unit in fluid_units:
            if unit not in {"oz", "ounce", "ounces"}:
                raise ValueError(
                    "Fluid ounces cannot be converted from weight."
                )

            multiplier = amount / float(serving_amount)

            if multiplier <= 0:
                raise ValueError(
                    "Calculated serving quantity was invalid."
                )

            return multiplier

        if normalized_serving_unit in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            serving_grams = float(serving_amount)
        elif normalized_serving_unit in {
            "oz",
            "ounce",
            "ounces",
        }:
            serving_grams = (
                float(serving_amount) * 28.349523125
            )
        else:
            raise ValueError(
                "This food serving cannot be converted "
                "safely from grams or ounces."
            )

        amount_grams = amount

        if unit in {"oz", "ounce", "ounces"}:
            amount_grams = amount * 28.349523125

        multiplier = (
            amount_grams / serving_grams
        )

        if multiplier <= 0:
            raise ValueError(
                "Calculated serving quantity was invalid."
            )

        return multiplier

    handful_phrase = None

    for candidate_phrase in (
        "small handful",
        "large handful",
        "handful",
    ):
        if candidate_phrase in description:
            handful_phrase = candidate_phrase
            break

    if handful_phrase:
        profile = get_portion_profile(
            food_id=int(food_id),
            phrase=handful_phrase,
        )

        if profile is None:
            raise ValueError(
                f"I do not have a saved {handful_phrase} amount "
                "for this food yet."
            )

        estimated_amount = float(
            profile["estimated_amount"]
        )

        estimated_unit = str(
            profile["estimated_unit"]
        ).strip().lower()

        if estimated_unit in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            amount_grams = estimated_amount
        elif estimated_unit in {
            "oz",
            "ounce",
            "ounces",
        }:
            amount_grams = (
                estimated_amount * 28.349523125
            )
        elif estimated_unit in {
            "serving",
            "servings",
        }:
            return estimated_amount
        else:
            raise ValueError(
                "The saved portion profile uses an unsupported unit."
            )

        if serving_amount is None or not serving_unit:
            raise ValueError(
                "The package serving size is unavailable."
            )

        normalized_serving_unit = (
            str(serving_unit).strip().lower()
        )

        if normalized_serving_unit in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            serving_grams = float(serving_amount)
        elif normalized_serving_unit in {
            "oz",
            "ounce",
            "ounces",
        }:
            serving_grams = (
                float(serving_amount) * 28.349523125
            )
        else:
            raise ValueError(
                "This saved portion profile cannot be converted "
                "safely to the food's base serving."
            )

        multiplier = (
            amount_grams / serving_grams
        )

        if multiplier <= 0:
            raise ValueError(
                "Saved portion quantity was invalid."
            )

        return multiplier

    raise ValueError(
        "I could not convert that packaged-food amount. "
        "Use a serving count, grams, or ounces."
    )


def resolve_non_restaurant_quantity(
    *,
    food_id: int,
    quantity: float | None,
    quantity_description: str | None,
    serving_amount: float | None,
    serving_unit: str | None,
    size: str | None = None,
) -> float:
    """
    Use plain numeric counts directly and convert only explicit portions.
    """
    description = str(
        quantity_description or ""
    ).strip().lower()
    normalized_serving_unit = str(
        serving_unit or ""
    ).strip().lower()

    if normalized_serving_unit in {
        "fl oz",
        "floz",
        "fluid ounce",
        "fluid ounces",
    }:
        size_description = str(size or "").strip().lower()
        fluid_match = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*"
            r"(?:fl\s*oz|fluid\s*ounces?|oz|ounces?)",
            size_description,
        )

        if fluid_match:
            description = f"{fluid_match.group(1)} oz"

    requires_conversion = bool(
        re.search(
            r"\b(?:servings?|g|grams?|grm|oz|ounces?|handful)\b",
            description,
        )
    )

    if not requires_conversion:
        return float(quantity or 1.0)

    return resolve_packaged_serving_multiplier(
        food_id=food_id,
        quantity=quantity,
        quantity_description=description,
        serving_amount=serving_amount,
        serving_unit=serving_unit,
    )


def format_display_number(
    value: float | int | None,
    *,
    decimals: int = 1,
) -> str:
    """Format nutrition numbers for human-readable Telegram output."""
    if value is None:
        return ""

    number = float(value)

    if number.is_integer():
        return str(int(number))

    if decimals == 0:
        return f"{number:.0f}"

    return f"{number:.{decimals}f}".rstrip("0").rstrip(".")


UNAVAILABLE_NUTRITION_INPUTS = {
    "n/a",
    "na",
    "none",
    "not available",
    "not known",
    "unavailable",
    "unknown",
}


def parse_nutrition_edit_number(
    value: str,
    *,
    allow_unavailable: bool,
) -> float | None:
    """Parse one nutrition correction without inventing missing values."""
    lowered = " ".join(str(value or "").strip().lower().split())
    if lowered in UNAVAILABLE_NUTRITION_INPUTS:
        if allow_unavailable:
            return None
        raise ValueError("Calories are required and cannot be unavailable.")

    cleaned = (
        lowered
        .replace(",", "")
        .replace("calories", "")
        .replace("calorie", "")
        .replace("cals", "")
        .replace("cal", "")
        .replace("grams", "")
        .replace("gram", "")
        .replace("mg", "")
        .replace("g", "")
        .strip()
    )
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValueError(
            "Enter a non-negative number or reply Unknown."
            if allow_unavailable
            else "Enter a non-negative calorie number."
        ) from exc
    if number < 0:
        raise ValueError("Nutrition values cannot be negative.")
    return number


def format_nutrition_edit_value(
    value: float | int | None,
    suffix: str,
) -> str:
    """Display a proposed nutrient while preserving unavailable values."""
    if value is None:
        return "not available"
    return f"{format_display_number(value)} {suffix}".strip()


def format_food_display_name(
    *,
    canonical_name: str,
    restaurant: str | None = None,
    size: str | None = None,
) -> str:
    """Return a friendlier food name without changing stored data."""
    name = canonical_name.strip()

    upper_restaurant_prefixes = {
        "BURGER KING,": "Burger King",
        "McDONALD'S,": "McDonald's",
    }

    packaged_brand_prefixes = {
        "SNYDER'S OF HANOVER,": "Snyder's of Hanover",
    }

    detected_restaurant = restaurant
    detected_brand = None

    for prefix, friendly_restaurant in upper_restaurant_prefixes.items():
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
            detected_restaurant = (
                detected_restaurant
                or friendly_restaurant
            )
            break

    if not detected_restaurant:
        for prefix, friendly_brand in packaged_brand_prefixes.items():
            if name.startswith(prefix):
                name = name[len(prefix):].strip()
                detected_brand = friendly_brand
                break

    replacements = {
        "french fries": "Fries",
        "French Fries": "Fries",
    }

    name = replacements.get(name, name)

    if name.islower() or name.isupper():
        name = name.title()

    parts = []

    if detected_restaurant:
        parts.append(detected_restaurant.strip())
    elif detected_brand:
        parts.append(detected_brand)

    if size:
        raw_size = str(size).strip()
        normalized_size = raw_size.title()
        lowered_size = raw_size.lower()

        quantity_like_size = (
            re.fullmatch(
                r"\d+(?:\.\d+)?\s*"
                r"(?:g|gram|grams|grm|oz|ounce|ounces|"
                r"serving|servings)",
                lowered_size,
            )
            is not None
            or lowered_size in {
                "a serving",
                "one serving",
                "half serving",
                "half a serving",
                "one half serving",
                "one and a half servings",
                "one and one half servings",
                "an ounce",
                "one ounce",
                "half ounce",
                "half an ounce",
                "one half ounce",
                "one and a half ounces",
                "one and one half ounces",
                "two ounces",
                "three ounces",
                "four ounces",
            }
        )

        if (
            not quantity_like_size
            and normalized_size.lower() not in name.lower()
        ):
            parts.append(normalized_size)

    parts.append(name)

    return " ".join(part for part in parts if part)


def format_nutrition_source(value: str | None) -> str:
    """Return a readable nutrition-source label."""
    normalized = (value or "").strip().lower()

    labels = {
        "fdc.nal.usda.gov": "USDA FoodData Central",
        "mcdonalds.com": "McDonald's official nutrition",
        "tacobell.com": "Taco Bell official nutrition",
        "burgerking.com": "Burger King official nutrition",
        "wendys.com": "Wendy's official nutrition",
        "world.openfoodfacts.org": "Open Food Facts",
        "openfoodfacts.org": "Open Food Facts",
        "user_package_label": "Package label entered by user",
    }

    return labels.get(
        normalized,
        value or "Verified saved nutrition",
    )


def format_pending_nutrition_confirmation(
    components: list[dict],
    *,
    meal_category: str,
    entry_date=None,
) -> str:
    """Format verified nutrition before Food Ledger logging."""
    lines = [
        "Verified nutrition:",
        "",
    ]

    date_label = format_food_entry_date(entry_date)
    if date_label:
        lines.extend([f"Date: {date_label}", ""])

    total_calories = 0.0
    total_protein = 0.0

    for component in components:
        quantity = float(component.get("quantity") or 1.0)
        calories = component.get("calories")
        protein = component.get("protein_g")

        display_name = format_food_display_name(
            canonical_name=component["canonical_name"],
            restaurant=component.get("restaurant"),
            size=component.get("size"),
        )

        line = (
            f"{component.get('role', 'Food')}: "
            f"{display_name}"
        )

        if quantity != 1:
            line += f" × {quantity:g}"

        if calories is not None:
            scaled_calories = float(calories) * quantity
            total_calories += scaled_calories
            line += (
                " — "
                + format_display_number(
                    scaled_calories,
                    decimals=0,
                )
                + " calories"
            )

        if protein is not None:
            total_protein += float(protein) * quantity

        lines.append(line)

        matched_by = component.get("matched_by")
        if matched_by in {"controlled_fallback", "pantry_name"}:
            requested_name = str(
                component.get("requested_food_name") or ""
            ).strip()
            if requested_name:
                match_label = (
                    "Pantry nutrition match"
                    if matched_by == "pantry_name"
                    else "Close Saved Food match"
                )
                lines.append(
                    f"{match_label}: {requested_name} → {display_name}"
                )
                lines.append(
                    "Review this match before choosing Log It."
                )

        source = component.get("verification_source")

        if source:
            lines.append(
                "Source: "
                + format_nutrition_source(source)
            )

    lines.extend(
        [
            "",
            f"Meal: {meal_category.title()}",
            (
                "Total calories: "
                + format_display_number(
                    total_calories,
                    decimals=0,
                )
            ),
            (
                "Total protein: "
                + format_display_number(
                    total_protein,
                    decimals=1,
                )
                + " g"
            ),
            "",
            "Nothing has been logged yet.",
            "",
            "1. Log It",
            "2. Enter custom nutrition",
            "3. Edit",
            "4. Cancel",
        ]
    )

    return "\n".join(lines)


def format_unresolved_food_review(
    item: dict,
    *,
    position: int,
    total: int,
) -> str:
    """Format one queued unresolved Food review prompt."""
    lines = [
        f"Unknown food {position} of {total}",
        "",
        f"Date: {item.get('entry_date') or 'Unknown'}",
        (
            "Meal: "
            + str(item.get("meal_category") or "Unknown").title()
        ),
        (
            "Food: "
            + str(item.get("food_name") or "Unknown food")
        ),
    ]

    if item.get("brand"):
        lines.append(f"Brand: {item['brand']}")

    if item.get("restaurant"):
        lines.append(f"Restaurant: {item['restaurant']}")

    if item.get("quantity_description"):
        lines.append(f"Amount: {item['quantity_description']}")
    elif item.get("quantity") is not None:
        lines.append(
            "Quantity: "
            + format_display_number(float(item["quantity"]))
        )

    lines.extend(
        [
            "",
            "Original message:",
            str(item.get("original_text") or ""),
            "",
            "1. Enter nutrition",
            "2. Change name/details",
            "3. Try automatic lookup again",
            "4. Skip for now",
            "5. Cancel this food",
        ]
    )

    return "\n".join(lines)


def show_unresolved_food_review(
    *,
    chat_id: int | str,
    pending: list[dict] | None = None,
    unresolved_food_id: int | None = None,
    skipped_ids: list[int] | None = None,
) -> bool:
    """Start review state and display one pending unresolved Food."""
    items = pending or get_pending_unresolved_foods()

    skipped = {int(value) for value in (skipped_ids or [])}
    available = [
        item
        for item in items
        if int(item["unresolved_food_id"]) not in skipped
    ]

    if not available:
        cancel_conversation(chat_id)
        send_telegram_msg(
            "There are no more unreviewed unknown foods "
            "in this session.",
            chat_id=chat_id,
        )
        return False

    position = 0

    if unresolved_food_id is not None:
        for index, candidate in enumerate(available):
            if int(candidate["unresolved_food_id"]) == int(
                unresolved_food_id
            ):
                position = index
                break

    item = available[position]

    start_conversation(
        chat_id=chat_id,
        conversation_type="unresolved_food_review",
        current_step="menu",
        known_data={
            "_unresolved_food_id": item["unresolved_food_id"],
            "_skipped_unresolved_food_ids": sorted(skipped),
        },
        missing_fields=[],
        original_message=str(item.get("original_text") or ""),
    )

    send_telegram_msg(
        format_unresolved_food_review(
            item,
            position=position + 1,
            total=len(available),
        ),
        chat_id=chat_id,
    )
    return True


def healthcoach_main_menu_text() -> str:
    return (
        "HealthCoach Menu\n\n"
        "1. Food\n"
        "2. Health\n"
        "3. Reports\n"
        "4. Help\n\n"
        "Reply cancel to close the menu."
    )


def healthcoach_food_menu_text() -> str:
    return (
        "Food Menu\n\n"
        "DAILY FOOD\n"
        "1. Log food\n"
        "2. Log food for yesterday\n"
        "3. Show today's food\n"
        "4. Edit today's food\n"
        "5. Undo last food\n"
        "6. Same as yesterday\n\n"
        "MY FOODS\n"
        "7. Favorites\n"
        "8. Food Library\n"
        "9. Saved recipes\n"
        "10. My Pantry\n\n"
        "TOOLS\n"
        "11. Photo tools\n"
        "12. Restaurant\n"
        "13. Update unknown foods\n"
        "14. Back"
    )


def healthcoach_photo_tools_menu_text() -> str:
    return (
        "Photo Tools Menu\n\n"
        "1. Read a restaurant menu photo\n"
        "2. Estimate an actual meal photo\n"
        "3. Scan a product barcode\n"
        "4. Back\n\n"
        "Nothing is saved or logged without your confirmation."
    )


def healthcoach_photo_intent_text() -> str:
    return (
        "What should I do with this photo?\n\n"
        "1. Estimate or log this meal\n"
        "2. Read a restaurant menu\n"
        "3. Scan a product barcode\n"
        "4. Add a scanned product to My Pantry\n"
        "5. Add visible items to My Pantry\n"
        "6. Import a recipe\n"
        "7. Cancel\n\n"
        "Nothing will be saved or logged without your confirmation."
    )


def photo_intent_from_caption(caption: str | None) -> str | None:
    """Return an explicit photo purpose supplied in its caption."""
    lowered = str(caption or "").strip().lower()

    if not lowered:
        return None

    if "recipe" in lowered and any(
        word in lowered for word in ("import", "read", "save")
    ):
        return "recipe"

    if is_food_photo_request(lowered):
        return "meal"

    if "pantry" in lowered and any(
        phrase in lowered
        for phrase in (
            "shelf",
            "cupboard",
            "visible items",
            "add items",
            "what i have",
        )
    ):
        return "pantry_photo"

    if "pantry" in lowered and (
        "barcode" in lowered or "scan product" in lowered
    ):
        return "pantry_barcode"

    if "barcode" in lowered or "scan product" in lowered:
        return "barcode"

    if "menu" in lowered and (
        "read" in lowered
        or "restaurant" in lowered
        or "recommend" in lowered
    ):
        return "menu"

    return None


def save_barcode_product_result(
    result: dict,
    *,
    barcode: str | None = None,
) -> dict:
    if not result.get("found"):
        raise ValueError(
            "Barcode nutrition is incomplete."
        )

    food = dict(result.get("food") or {})
    nutrition = dict(result.get("nutrition") or {})
    verification = dict(
        result.get("verification") or {}
    )

    if nutrition.get("calories") is None:
        raise ValueError(
            "Barcode calories are unavailable."
        )

    verification_status = str(
        verification.get("status") or ""
    )
    verification_source = str(
        verification.get("source") or ""
    ).strip()

    # Community nutrition becomes trusted only after the user
    # explicitly chooses Save Product or Log It.
    if verification_status != "verified":
        stored_status = "verified"
        stored_source = "user_entered"
    else:
        stored_status = "verified"
        stored_source = (
            verification_source or "barcode"
        )

    saved = add_food_with_nutrition(
        canonical_name=str(
            food.get("canonical_name")
            or "Scanned product"
        ),
        serving_description=str(
            food.get("serving_description")
            or "1 serving"
        ),
        serving_amount=float(
            food.get("serving_amount") or 1.0
        ),
        serving_unit=str(
            food.get("serving_unit") or "serving"
        ),
        verification_status=stored_status,
        verification_source=stored_source,
        calories=float(nutrition["calories"]),
        protein_g=nutrition.get("protein_g"),
        carbohydrates_g=nutrition.get(
            "carbohydrates_g"
        ),
        fat_g=nutrition.get("fat_g"),
        fiber_g=nutrition.get("fiber_g"),
        sugar_g=nutrition.get("sugar_g"),
        sodium_mg=nutrition.get("sodium_mg"),
        brand=food.get("brand"),
        restaurant=None,
        food_type=str(
            food.get("food_type") or "food"
        ),
        source_item_id=verification.get(
            "source_item_id"
        ),
        source_url=verification.get("source_url"),
    )

    if barcode:
        save_barcode_mapping(
            barcode=barcode,
            food_id=int(saved["food"]["food_id"]),
        )

    return saved


def build_taught_barcode_result(
    *,
    barcode: str,
    product_name: str,
    brand: str | None,
    label: dict,
) -> dict:
    """Convert a user-confirmed label reading into a barcode result."""
    return {
        "found": True,
        "provider": "user_package_label",
        "food": {
            "canonical_name": product_name.strip(),
            "restaurant": None,
            "brand": (brand or "").strip() or None,
            "food_type": "food",
            "serving_description": label["serving_description"],
            "serving_amount": float(label["serving_amount"]),
            "serving_unit": label["serving_unit"],
        },
        "nutrition": {
            "calories": float(label["calories"]),
            "protein_g": float(label["protein_g"]),
            "carbohydrates_g": float(label["carbohydrates_g"]),
            "fat_g": float(label["fat_g"]),
            "fiber_g": float(label["fiber_g"]),
            "sugar_g": float(label["sugar_g"]),
            "sodium_mg": float(label["sodium_mg"]),
        },
        "verification": {
            "status": "verified",
            "source": "user_package_label",
            "source_item_id": barcode,
            "source_url": None,
        },
        "missing_fields": [],
        "clarification_question": None,
        "notes": [
            "Nutrition was read from the package label and "
            "confirmed by the user."
        ],
    }


def format_barcode_teaching_confirmation(
    *,
    barcode: str,
    product_name: str,
    brand: str | None,
    label: dict,
) -> str:
    """Format the final review before saving a taught barcode."""
    result = build_taught_barcode_result(
        barcode=barcode,
        product_name=product_name,
        brand=brand,
        label=label,
    )
    product_text = format_barcode_product(
        result,
        barcode=barcode,
    )
    product_text = product_text.replace(
        "Barcode Product\n\n",
        "Teach This Barcode\n\n",
        1,
    ).replace(
        "Nothing has been saved or logged.\n\n"
        "Reply Save Product, Log It, Scan Another, Back, or Cancel.",
        "Save this product and teach HealthCoach this barcode?\n\n"
        "Previously logged food will not change.\n\n"
        "1. Yes, save it\n"
        "2. Retake label photo\n"
        "3. Cancel",
    )
    return product_text


def format_barcode_product(
    result: dict,
    *,
    barcode: str,
    saved: bool = False,
    pantry_item_name: str | None = None,
) -> str:
    food = dict(result.get("food") or {})
    nutrition = dict(result.get("nutrition") or {})
    verification = dict(result.get("verification") or {})

    def nutrient(field: str, suffix: str) -> str:
        value = nutrition.get(field)
        if value is None:
            return "not available"
        return (
            f"{format_display_number(float(value))} {suffix}"
        ).strip()

    lines = [
        "Barcode Product",
        "",
        f"Product: {food.get('canonical_name') or 'Unknown product'}",
    ]

    if food.get("brand"):
        lines.append(f"Brand: {food['brand']}")

    source = str(
        verification.get("source") or "USDA"
    ).strip()
    source = {
        "user_package_label": "Package label entered by user",
        "user_entered": "User-entered nutrition",
    }.get(source, source)

    lines.extend([
        f"Barcode: {barcode}",
        (
            "Serving: "
            f"{food.get('serving_description') or 'not available'}"
        ),
        f"Source: {source}",
        "",
        f"Calories: {nutrient('calories', 'cal')}",
        f"Protein: {nutrient('protein_g', 'g')}",
        (
            "Carbohydrates: "
            f"{nutrient('carbohydrates_g', 'g')}"
        ),
        f"Fat: {nutrient('fat_g', 'g')}",
        f"Fiber: {nutrient('fiber_g', 'g')}",
        f"Sugar: {nutrient('sugar_g', 'g')}",
        f"Sodium: {nutrient('sodium_mg', 'mg')}",
        "",
        (
            "This product is saved. Nothing has been logged."
            if saved
            else "Nothing has been saved or logged."
        ),
        "",
        (
            "Reply Link nutrition, Save Product, Scan Another, Back, "
            "or Cancel."
            if pantry_item_name
            else (
                "Reply Add to Pantry, Save Product, Log It, "
                "Scan Another, Back, or Cancel."
            )
        ),
    ])

    if pantry_item_name:
        lines.insert(
            -2,
            f"Selected Pantry item: {pantry_item_name}",
        )

    return "\n".join(lines)


def healthcoach_saved_foods_menu_text() -> str:
    return (
        "Food Library Menu\n\n"
        "1. Find a food\n"
        "2. Browse entered foods\n"
        "3. Add entered food\n"
        "4. Edit entered food\n"
        "5. Remove entered food\n"
        "6. Review possible duplicates\n"
        "7. Library health check\n"
        "8. Back\n\n"
        "Food Library search includes USDA, package-label, barcode, "
        "recipe, and manually entered nutrition records. Entered foods "
        "are the records you created and can edit directly."
    )


def healthcoach_saved_recipes_menu_text() -> str:
    return (
        "Saved Recipes Menu\n\n"
        "1. Browse saved recipes\n"
        "2. Create recipe\n"
        "3. Import recipe\n"
        "4. Edit saved recipe\n"
        "5. Delete saved recipe\n"
        "6. Back\n\n"
        "Import pasted recipe text or a recipe photo, create recipes "
        "from Food Library records, or save them from Pantry meal ideas. "
        "Saving a recipe does not log it as eaten."
    )


def format_saved_recipe_choices(recipes: list[dict]) -> str:
    lines = ["Choose a saved recipe to view:", ""]
    for index, recipe in enumerate(recipes, start=1):
        heart_label = (
            " — Heart-Healthy Pick"
            if recipe.get("heart_healthy_pick")
            else ""
        )
        lines.append(
            f"{index}. {recipe.get('canonical_name') or 'Recipe'} — "
            f"{str(recipe.get('meal_type') or 'meal').title()}, "
            f"{format_display_number(float(recipe.get('calories') or 0), decimals=0)} cal"
            f"{heart_label}"
        )
    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


def format_saved_recipe_management_choices(
    recipes: list[dict],
    *,
    action: str,
) -> str:
    verb = "edit" if action == "edit" else "delete"
    lines = [f"Choose a saved recipe to {verb}:", ""]
    for index, recipe in enumerate(recipes, start=1):
        heart_label = (
            " — Heart-Healthy Pick"
            if recipe.get("heart_healthy_pick")
            else ""
        )
        lines.append(
            f"{index}. {recipe.get('canonical_name') or 'Recipe'} — "
            f"{str(recipe.get('meal_type') or 'meal').title()}, "
            f"{format_display_number(float(recipe.get('calories') or 0), decimals=0)} cal"
            f"{heart_label}"
        )
    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


def format_saved_recipe_edit_menu(recipe: dict) -> str:
    heart_status = (
        "Heart-Healthy Pick"
        if recipe.get("heart_healthy_pick")
        else "not labeled"
    )
    return (
        "Saved Recipe Edit Menu\n\n"
        f"Recipe: {recipe.get('canonical_name') or 'Saved recipe'}\n"
        f"Current nutrition version: "
        f"{int(recipe.get('version_number') or 1)}\n"
        f"Heart-health label: {heart_status}\n\n"
        "1. Name\n"
        "2. Meal type\n"
        "3. Summary\n"
        "4. Ingredients\n"
        "5. Preparation\n"
        "6. Nutrition\n"
        "7. Back\n\n"
        "Nutrition changes apply only to future logs."
    )


def parse_saved_recipe_ingredients(value: str) -> list[dict]:
    lines = [
        line.strip().lstrip("- ").strip()
        for line in re.split(r"[\n;]+", str(value or ""))
        if line.strip().lstrip("- ").strip()
    ]
    ingredients = []
    for line in lines:
        if "|" not in line:
            raise ValueError(
                "Use amount | ingredient for every line."
            )
        amount, name = [part.strip() for part in line.split("|", 1)]
        if not amount or not name:
            raise ValueError(
                "Every ingredient needs both an amount and a name."
            )
        ingredients.append({
            "name": name,
            "amount": amount,
            "source": "additional",
        })
    if not ingredients:
        raise ValueError("Enter at least one ingredient.")
    return ingredients


def parse_saved_recipe_steps(value: str) -> list[str]:
    steps = [
        re.sub(r"^\d+[.)]\s*", "", line.strip()).strip()
        for line in re.split(r"[\n;]+", str(value or ""))
        if line.strip()
    ]
    steps = [step for step in steps if step]
    if not steps:
        raise ValueError("Enter at least one preparation step.")
    return steps


RECIPE_BUILDER_PANTRY_PAGE_SIZE = 10


def format_recipe_builder_food_choices(
    foods: list[dict],
    *,
    page: int = 1,
    total_pages: int = 1,
    nutrition_needed_count: int = 0,
) -> str:
    lines = [
        "Recipe Builder — Choose from My Pantry",
        "",
    ]
    for index, food in enumerate(foods, start=1):
        lines.append(
            f"{index}. {food.get('canonical_name') or 'Saved Food'} — "
            f"{food.get('serving_description') or '1 serving'}"
        )
    if not foods:
        lines.append("No Pantry items have complete saved nutrition yet.")

    lines.extend(["", f"Page {page} of {total_pages}"])
    if nutrition_needed_count:
        lines.append(
            f"{nutrition_needed_count} other Pantry item(s) need saved "
            "nutrition before Recipe Builder can calculate them."
        )
    lines.extend([
        "",
        "Choose a number, reply Next or Previous, type a Saved Food "
        "name, or reply Add new saved food.",
        "Reply Back to return.",
    ])
    return "\n".join(lines)


def show_recipe_builder_pantry_choices(
    *,
    chat_id: int | str,
    requested_page: int = 1,
) -> None:
    """Show one numbered page of nutrition-ready Pantry ingredients."""
    pantry_items = list_recipe_pantry_foods()
    ready = [
        item for item in pantry_items if item.get("nutrition_ready")
    ]
    nutrition_needed_count = len(pantry_items) - len(ready)
    total_pages = max(
        1,
        (
            len(ready) + RECIPE_BUILDER_PANTRY_PAGE_SIZE - 1
        ) // RECIPE_BUILDER_PANTRY_PAGE_SIZE,
    )
    page = min(max(int(requested_page), 1), total_pages)
    start = (page - 1) * RECIPE_BUILDER_PANTRY_PAGE_SIZE
    foods = ready[start : start + RECIPE_BUILDER_PANTRY_PAGE_SIZE]

    update_conversation(
        chat_id=chat_id,
        current_step="recipe_builder_ingredient_select",
        known_data={
            "_recipe_builder_pantry_page": page,
            "_recipe_builder_pantry_total_pages": total_pages,
            "_recipe_builder_food_ids": [
                int(food["food_id"]) for food in foods
            ],
        },
        missing_fields=["ingredient"],
    )
    send_telegram_msg(
        format_recipe_builder_food_choices(
            foods,
            page=page,
            total_pages=total_pages,
            nutrition_needed_count=nutrition_needed_count,
        ),
        chat_id=chat_id,
    )


def format_recipe_builder_ingredients(
    known_data: dict,
) -> str:
    ingredients = list(
        known_data.get("_recipe_builder_ingredients") or []
    )
    lines = ["Recipe Builder — Ingredients", ""]
    for index, ingredient in enumerate(ingredients, start=1):
        lines.append(
            f"{index}. {ingredient.get('amount_description')} "
            f"{ingredient.get('name')}"
        )
    calculation = calculate_recipe_nutrition(
        ingredients,
        yield_servings=float(
            known_data.get("_recipe_builder_yield") or 1
        ),
    )
    total = calculation["total"]
    per_serving = calculation["per_serving"]
    lines.extend([
        "",
        "Recipe total so far: "
        f"{format_display_number(total['calories'], decimals=0)} cal",
        "Per serving so far: "
        f"{format_display_number(per_serving['calories'], decimals=0)} cal, "
        f"{format_display_number(per_serving['protein_g'])} g protein",
        "",
        "1. Add another ingredient",
        "2. Continue",
        "3. Remove last ingredient",
        "4. Cancel",
    ])
    return "\n".join(lines)


def format_recipe_builder_edit_choices(known_data: dict) -> str:
    """Show numbered draft ingredients that can be corrected before save."""
    ingredients = list(
        known_data.get("_recipe_builder_ingredients") or []
    )
    lines = [
        "Recipe Builder — Edit Ingredients",
        "",
        "Choose an ingredient to correct:",
        "",
    ]
    for index, ingredient in enumerate(ingredients, start=1):
        lines.append(
            f"{index}. {ingredient.get('amount_description')} "
            f"{ingredient.get('name')}"
        )
    lines.extend([
        "",
        "Reply with an ingredient number, Add ingredient, Back to review, "
        "or Cancel.",
    ])
    return "\n".join(lines)


def format_recipe_builder_edit_action(known_data: dict) -> str:
    """Show the safe corrections available for one draft ingredient."""
    ingredients = list(
        known_data.get("_recipe_builder_ingredients") or []
    )
    index_value = known_data.get("_recipe_builder_edit_index")
    index = int(index_value) if index_value is not None else -1
    if index < 0 or index >= len(ingredients):
        raise ValueError("That recipe ingredient is no longer available.")
    ingredient = dict(ingredients[index])
    lines = [
        "Recipe Builder — Edit Ingredient",
        "",
        f"Ingredient: {ingredient.get('name')}",
        f"Entire-recipe amount: {ingredient.get('amount_description')}",
        f"Saved serving: {ingredient.get('serving_description') or 'not listed'}",
        "",
        "1. Change amount",
        "2. Change Saved Food",
    ]
    if not (
        known_data.get("_recipe_import_active")
        and ingredient.get("_recipe_import_source_label")
        and not ingredient.get("_recipe_import_optional")
    ):
        lines.append("3. Remove ingredient")
    else:
        lines.append(
            "3. Remove ingredient — unavailable for a required imported "
            "ingredient"
        )
    lines.extend(["4. Back", "", "Nothing has been changed yet."])
    return "\n".join(lines)


def show_recipe_builder_edit_choices(
    *,
    chat_id: int | str,
    known_data: dict,
) -> None:
    """Return to the numbered correction list without changing nutrition."""
    updated = dict(known_data)
    # Conversation updates merge dictionaries, so explicitly clear transient
    # selections instead of only removing them from this local copy.
    updated["_recipe_builder_edit_index"] = None
    updated["_recipe_builder_replace_index"] = None
    updated["_recipe_builder_selected_food_id"] = None
    updated["_recipe_builder_review_editing"] = True
    update_conversation(
        chat_id=chat_id,
        current_step="recipe_builder_edit_select",
        known_data=updated,
        missing_fields=[],
    )
    send_telegram_msg(
        format_recipe_builder_edit_choices(updated),
        chat_id=chat_id,
    )


def show_recipe_builder_edit_action(
    *,
    chat_id: int | str,
    known_data: dict,
) -> None:
    """Return to one selected ingredient's correction menu."""
    update_conversation(
        chat_id=chat_id,
        current_step="recipe_builder_edit_action",
        known_data=known_data,
        missing_fields=[],
    )
    send_telegram_msg(
        format_recipe_builder_edit_action(known_data),
        chat_id=chat_id,
    )


def format_recipe_import_missing(ingredient: dict) -> str:
    name = str(ingredient.get("ingredient_name") or "ingredient")
    amount = str(ingredient.get("amount_description") or "").strip()
    brand = str(ingredient.get("brand") or "").strip()
    label = " ".join(part for part in (amount, brand, name) if part)
    lines = [
        "Recipe Import — Ingredient Needed",
        "",
        label,
    ]
    if (
        ingredient.get("candidate_food_id")
        and not ingredient.get("conversion_error")
        and (ingredient.get("optional") or ingredient.get("trace_only"))
    ):
        lines.extend([
            "",
            "Matched Saved Food: "
            + str(ingredient.get("candidate_food_name") or "Saved Food"),
            "Reply Include ingredient or Exclude ingredient.",
        ])
    elif ingredient.get("candidate_food_id"):
        lines.extend([
            "",
            "I found a Saved Food, but its serving could not be safely "
            "matched to this recipe amount.",
            "Saved Food: "
            + str(ingredient.get("candidate_food_name") or "Saved Food"),
            "Saved Food serving: "
            + str(
                ingredient.get("candidate_serving_description")
                or "not listed"
            ),
            "Reply Use one Saved Food serving only when the entire recipe "
            "uses exactly one listed serving. Otherwise, reply Amount: "
            "followed by an equivalent amount.",
            "Example: Amount: 2 servings.",
        ])
    else:
        lines.extend([
            "",
            "I could not match this to one nutrition-ready Saved Food.",
            "Type the exact name of an existing Saved Food, or reply "
            "Try verified lookup or Add new saved food.",
        ])
    if ingredient.get("optional") or ingredient.get("trace_only"):
        lines.append(
            "You may also reply Exclude ingredient. It will be listed "
            "as excluded from the nutrition calculation."
        )
    lines.extend([
        "",
        "A major ingredient cannot be silently skipped.",
        "Reply Cancel to stop without saving.",
    ])
    return "\n".join(lines)


def format_recipe_import_saved_food_confirmation(
    *,
    ingredient: dict,
    food: dict,
) -> str:
    amount = str(ingredient.get("amount_description") or "").strip()
    brand = str(ingredient.get("brand") or "").strip()
    name = str(ingredient.get("ingredient_name") or "ingredient").strip()
    source_label = " ".join(
        part for part in (amount, brand, name) if part
    )
    serving = str(food.get("serving_description") or "not listed")
    return (
        "Recipe Import — Confirm Saved Food\n\n"
        f"Imported ingredient: {source_label}\n"
        f"Selected Saved Food: {food.get('canonical_name')}\n"
        f"Saved Food serving: {serving}\n\n"
        "Use this Saved Food for this imported ingredient?\n\n"
        "This confirmation also protects against a reply arriving after "
        "Recipe Import has moved to the next ingredient.\n\n"
        "1. Yes\n"
        "2. No"
    )


RECIPE_IMPORT_REQUIRED_NUTRIENTS = (
    "calories",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
)


def recipe_import_lookup_is_complete(result: dict) -> bool:
    nutrition = dict(result.get("nutrition") or {})
    return bool(
        result.get("found")
        and not any(
            nutrition.get(field) is None
            for field in RECIPE_IMPORT_REQUIRED_NUTRIENTS
        )
    )


def format_recipe_import_lookup_failure(ingredient: dict) -> str:
    name = str(ingredient.get("ingredient_name") or "ingredient")
    amount = str(ingredient.get("amount_description") or "").strip()
    suggestion = str(ingredient.get("generic_lookup_name") or "").strip()
    lines = [
        "Recipe Import — Verified Lookup Options",
        "",
        "I couldn't find one complete verified match for:",
        f"{amount} {name}".strip(),
    ]
    notes = [
        str(note).strip()
        for note in (ingredient.get("lookup_failure_notes") or [])
        if str(note).strip()
    ][:2]
    if notes:
        lines.extend(["", "Provider details:"])
        lines.extend(f"- {note[:240]}" for note in notes)
    lines.extend(["", "Nothing was saved."])
    if suggestion:
        lines.append(f"Try verified generic match: {suggestion}")
    lines.extend([
        "Enter a simpler description",
        "Add new saved food",
        "Cancel",
    ])
    return "\n".join(lines)


def show_recipe_import_verified_confirmation(
    *,
    chat_id: int,
    known_data: dict,
    provider_result: dict,
    lookup_name: str,
    generic_match: bool,
) -> None:
    provider_food = dict(provider_result.get("food") or {})
    nutrition = dict(provider_result.get("nutrition") or {})
    updated = dict(known_data)
    updated["_recipe_import_verified_result"] = provider_result
    updated["_recipe_import_verified_lookup_name"] = lookup_name
    updated["_recipe_import_verified_is_generic"] = bool(generic_match)
    update_conversation(
        chat_id=chat_id,
        current_step="recipe_import_verified_confirmation",
        known_data=updated,
        missing_fields=[],
    )
    source = str(
        (provider_result.get("verification") or {}).get("source")
        or "verified source"
    )
    match_note = (
        "\nMatch type: Verified generic match approved for review.\n"
        if generic_match
        else ""
    )
    send_telegram_msg(
        "Verified ingredient found:\n\n"
        f"Food: {provider_food.get('canonical_name')}\n"
        f"Serving: {provider_food.get('serving_description')}\n"
        f"Calories: {format_display_number(float(nutrition['calories']), decimals=0)}\n"
        f"Protein: {format_display_number(float(nutrition['protein_g']))} g\n"
        f"Source: {source}\n"
        f"{match_note}\n"
        "Save this as a Saved Food and use it in the recipe?\n\n"
        "1. Yes\n2. No",
        chat_id=chat_id,
    )


def run_recipe_import_verified_lookup(
    *,
    chat_id: int,
    known_data: dict,
    lookup_name: str,
    generic_match: bool = False,
) -> None:
    pending = list(known_data.get("_recipe_import_pending") or [])
    if not pending:
        continue_recipe_import_resolution(chat_id=chat_id, known_data=known_data)
        return
    current = dict(pending[0])
    send_telegram_msg(
        "I'm checking for one verified nutrition match. "
        "This may take a moment.",
        chat_id=chat_id,
    )
    try:
        provider_result = lookup_official_nutrition(
            restaurant=None,
            food_name=str(lookup_name or "").strip(),
            size=str(current.get("amount_description") or ""),
            brand=None if generic_match else current.get("brand"),
        )
    except Exception:
        logging.exception("Recipe ingredient lookup failed")
        provider_result = {
            "found": False,
            "notes": ["The verified nutrition provider was unavailable."],
        }

    if not recipe_import_lookup_is_complete(provider_result):
        suggestion = suggest_generic_ingredient_name(
            str(current.get("ingredient_name") or ""),
            brand=current.get("brand"),
        )
        if generic_match or suggestion == str(lookup_name).strip().lower():
            suggestion = None
        current["generic_lookup_name"] = suggestion
        current["lookup_failure_notes"] = list(
            provider_result.get("notes") or []
        )
        current["lookup_attempt_name"] = str(lookup_name).strip()
        updated = dict(known_data)
        updated["_recipe_import_pending"] = [current, *pending[1:]]
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_ingredient",
            known_data=updated,
            missing_fields=["recipe_ingredient"],
        )
        send_telegram_msg(
            format_recipe_import_lookup_failure(current),
            chat_id=chat_id,
        )
        return

    provider_food = dict(provider_result.get("food") or {})
    try:
        ingredient_serving_multiplier(
            amount_description=str(current.get("amount_description") or ""),
            serving_amount=float(provider_food["serving_amount"]),
            serving_unit=str(provider_food["serving_unit"]),
            serving_description=str(
                provider_food.get("serving_description") or ""
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        updated = dict(known_data)
        updated["_recipe_import_verified_result"] = provider_result
        updated["_recipe_import_verified_lookup_name"] = str(lookup_name)
        updated["_recipe_import_verified_is_generic"] = bool(generic_match)
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_verified_amount",
            known_data=updated,
            missing_fields=["recipe_ingredient_amount"],
        )
        send_telegram_msg(
            "Recipe Import — Verified Serving\n\n"
            f"Recipe amount: {current.get('amount_description')}\n"
            f"Verified serving: {provider_food.get('serving_description')}\n\n"
            f"{exc}\n\n"
            "The verified result has been preserved. Reply Use one verified "
            "serving, or type Amount: followed by a compatible amount. "
            "Reply Back to choose another match.",
            chat_id=chat_id,
        )
        return

    show_recipe_import_verified_confirmation(
        chat_id=chat_id,
        known_data=known_data,
        provider_result=provider_result,
        lookup_name=str(lookup_name),
        generic_match=generic_match,
    )


def continue_recipe_import_resolution(
    *,
    chat_id: int,
    known_data: dict,
) -> None:
    pending = list(known_data.get("_recipe_import_pending") or [])
    updated = dict(known_data)
    updated["_recipe_import_pending"] = pending
    if pending:
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_ingredient",
            known_data=updated,
            missing_fields=["recipe_ingredient"],
        )
        send_telegram_msg(
            format_recipe_import_missing(pending[0]),
            chat_id=chat_id,
        )
        return

    update_conversation(
        chat_id=chat_id,
        current_step="recipe_builder_confirmation",
        known_data=updated,
        missing_fields=[],
    )
    send_telegram_msg(
        format_recipe_builder_review(updated),
        chat_id=chat_id,
    )


def annotate_imported_recipe_ingredient(
    ingredient: dict,
    source: dict,
) -> dict:
    """Keep source optionality in the draft without persisting it as food data."""
    annotated = dict(ingredient)
    source_label = " ".join(
        part
        for part in (
            str(source.get("amount_description") or "").strip(),
            str(source.get("brand") or "").strip(),
            str(source.get("ingredient_name") or "").strip(),
        )
        if part
    )
    annotated["_recipe_import_source_label"] = source_label
    annotated["_recipe_import_optional"] = bool(
        source.get("optional") or source.get("trace_only")
    )
    return annotated


def resolve_recipe_import_ingredients(
    *,
    chat_id: int,
    known_data: dict,
) -> None:
    resolved = list(known_data.get("_recipe_builder_ingredients") or [])
    pending = []
    for source in list(known_data.get("_recipe_import_source_ingredients") or []):
        item = dict(source)
        name = str(item.get("ingredient_name") or "").strip()
        brand = str(item.get("brand") or "").strip()
        food = (
            find_recipe_ingredient_food(f"{brand} {name}".strip())
            if brand
            else None
        ) or find_recipe_ingredient_food(name)
        if food is not None:
            item["candidate_food_id"] = int(food["food_id"])
            item["candidate_food_name"] = str(food["canonical_name"])
        if food is not None and not (
            item.get("optional") or item.get("trace_only")
        ):
            try:
                resolved.append(
                    annotate_imported_recipe_ingredient(
                        prepare_recipe_ingredient(
                            food_id=int(food["food_id"]),
                            amount_description=str(
                                item.get("amount_description") or ""
                            ),
                        ),
                        item,
                    )
                )
                continue
            except (TypeError, ValueError) as exc:
                item["conversion_error"] = str(exc)
        pending.append(item)

    updated = dict(known_data)
    updated["_recipe_builder_ingredients"] = resolved
    updated["_recipe_import_pending"] = pending
    continue_recipe_import_resolution(chat_id=chat_id, known_data=updated)


def advance_recipe_import_metadata(
    *,
    chat_id: int,
    known_data: dict,
) -> None:
    if not str(known_data.get("_recipe_builder_name") or "").strip():
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_name",
            known_data=known_data,
            missing_fields=["recipe_name"],
        )
        send_telegram_msg(
            "Recipe Import\n\nWhat should this recipe be called?",
            chat_id=chat_id,
        )
        return
    if str(known_data.get("_recipe_builder_meal_type") or "") not in {
        "lunch",
        "dinner",
    }:
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_meal_type",
            known_data=known_data,
            missing_fields=["meal_type"],
        )
        send_telegram_msg(
            "Should this imported recipe be for lunch or dinner?\n\n"
            "1. Lunch\n2. Dinner",
            chat_id=chat_id,
        )
        return
    if not known_data.get("_recipe_builder_yield"):
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_yield",
            known_data=known_data,
            missing_fields=["yield_servings"],
        )
        send_telegram_msg(
            "How many servings does the entire imported recipe make?",
            chat_id=chat_id,
        )
        return
    if not list(known_data.get("_recipe_builder_steps") or []):
        update_conversation(
            chat_id=chat_id,
            current_step="recipe_import_preparation",
            known_data=known_data,
            missing_fields=["preparation_steps"],
        )
        send_telegram_msg(
            "I couldn't find complete preparation directions. Enter the "
            "steps, one per line.",
            chat_id=chat_id,
        )
        return
    resolve_recipe_import_ingredients(chat_id=chat_id, known_data=known_data)


def start_recipe_import_draft(
    *,
    chat_id: int,
    draft: dict,
) -> None:
    if not draft.get("readable") or not draft.get("ingredients"):
        send_telegram_msg(
            "I couldn't extract a reliable recipe from that. Try pasted "
            "recipe text or a clearer photo showing the full ingredient "
            "list. Nothing was saved.",
            chat_id=chat_id,
        )
        return
    meal_type = str(draft.get("meal_type") or "").lower()
    if meal_type not in {"lunch", "dinner"}:
        meal_type = None
    data = {
        "_recipe_import_active": True,
        "_recipe_builder_name": draft.get("recipe_name"),
        "_recipe_builder_existing_id": None,
        "_recipe_builder_meal_type": meal_type,
        "_recipe_builder_yield": draft.get("yield_servings"),
        "_recipe_import_claimed_calories": draft.get(
            "claimed_calories_per_serving"
        ),
        "_recipe_import_claimed_protein_g": draft.get(
            "claimed_protein_g_per_serving"
        ),
        "_recipe_builder_ingredients": [],
        "_recipe_builder_summary": str(draft.get("summary") or ""),
        "_recipe_builder_steps": list(draft.get("preparation_steps") or []),
        "_recipe_import_source_ingredients": list(
            draft.get("ingredients") or []
        ),
        "_recipe_import_pending": [],
        "_recipe_import_excluded": [],
        "_recipe_import_created_food_ids": [],
    }
    advance_recipe_import_metadata(chat_id=chat_id, known_data=data)


def format_recipe_builder_review(known_data: dict) -> str:
    ingredients = list(
        known_data.get("_recipe_builder_ingredients") or []
    )
    yield_servings = float(
        known_data.get("_recipe_builder_yield") or 1
    )
    calculation = calculate_recipe_nutrition(
        ingredients,
        yield_servings=yield_servings,
    )
    total = calculation["total"]
    per_serving = calculation["per_serving"]
    is_update = bool(known_data.get("_recipe_builder_existing_id"))
    lines = [
        "Recipe Builder Review",
        "",
        f"Recipe: {known_data.get('_recipe_builder_name')}",
        "Meal type: "
        f"{str(known_data.get('_recipe_builder_meal_type')).title()}",
        "Makes: "
        f"{format_display_number(yield_servings)} serving(s)",
        "",
        "Ingredients:",
    ]
    for ingredient in ingredients:
        lines.append(
            f"- {ingredient.get('amount_description')} "
            f"{ingredient.get('name')}"
        )
    excluded = list(known_data.get("_recipe_import_excluded") or [])
    if excluded:
        lines.extend(["", "Excluded from nutrition with your approval:"])
        lines.extend(f"- {item}" for item in excluded)
    claimed_calories = known_data.get("_recipe_import_claimed_calories")
    claimed_protein = known_data.get("_recipe_import_claimed_protein_g")
    if claimed_calories is not None or claimed_protein is not None:
        lines.extend(["", "Source nutrition comparison:"])
        if claimed_calories is not None:
            lines.append(
                "- Source claims "
                f"{format_display_number(float(claimed_calories), decimals=0)} "
                "calories per serving; Saved Food calculation is "
                f"{format_display_number(per_serving['calories'], decimals=0)}."
            )
        if claimed_protein is not None:
            lines.append(
                "- Source claims "
                f"{format_display_number(float(claimed_protein))} g protein "
                "per serving; Saved Food calculation is "
                f"{format_display_number(per_serving['protein_g'])} g."
            )
        lines.append(
            "Source claims are comparison-only and never replace the Saved "
            "Food calculation. Review differences before saving."
        )
    lines.extend([
        "",
        "Important: every ingredient amount above is the total used in the "
        "entire recipe, not the amount in each finished serving.",
        "Choose Change ingredients if any amount is a placeholder or does "
        "not match the full recipe.",
        "",
        "Entire recipe:",
        f"Calories: {format_display_number(total['calories'], decimals=0)}",
        f"Protein: {format_display_number(total['protein_g'])} g",
        f"Carbohydrates: {format_display_number(total['carbohydrates_g'])} g",
        f"Fat: {format_display_number(total['fat_g'])} g",
        f"Fiber: {format_display_number(total['fiber_g'])} g",
        f"Sugar: {format_display_number(total['sugar_g'])} g",
        f"Sodium: {format_display_number(total['sodium_mg'], decimals=0)} mg",
        "",
        "Per serving:",
        f"Calories: {format_display_number(per_serving['calories'], decimals=0)}",
        f"Protein: {format_display_number(per_serving['protein_g'])} g",
        f"Carbohydrates: {format_display_number(per_serving['carbohydrates_g'])} g",
        f"Fat: {format_display_number(per_serving['fat_g'])} g",
        f"Fiber: {format_display_number(per_serving['fiber_g'])} g",
        f"Sugar: {format_display_number(per_serving['sugar_g'])} g",
        f"Sodium: {format_display_number(per_serving['sodium_mg'], decimals=0)} mg",
        "",
        "Nutrition is calculated from the exact Saved Food versions "
        "selected above.",
        "Nothing has been saved or logged yet.",
        "",
        "1. Save Changes" if is_update else "1. Save Recipe",
        "2. Change ingredients",
        "3. Cancel",
    ])
    return "\n".join(lines)


def format_saved_recipe_details(recipe: dict) -> str:
    lines = [
        "Saved Recipe Details",
        "",
        str(recipe.get("canonical_name") or "Saved recipe"),
        str(recipe.get("summary") or ""),
    ]
    if recipe.get("heart_healthy_pick"):
        lines.extend([
            "",
            "Heart-Healthy Pick",
            str(recipe.get("heart_healthy_reason") or ""),
            "This is a food-choice label, not a medical rating.",
        ])
    lines.extend([
        "",
        "Recipe yield: "
        f"{format_display_number(float(recipe.get('yield_servings') or 1))} serving(s)",
        "Estimated nutrition for 1 serving:",
        "Calories: "
        f"{format_display_number(float(recipe.get('calories') or 0), decimals=0)}",
        "Protein: "
        f"{format_display_number(float(recipe.get('protein_g') or 0))} g",
        "Carbohydrates: "
        f"{format_display_number(float(recipe.get('carbohydrates_g') or 0))} g",
        "Fat: "
        f"{format_display_number(float(recipe.get('fat_g') or 0))} g",
        "Fiber: "
        f"{format_display_number(float(recipe.get('fiber_g') or 0))} g",
        "Sugar: "
        f"{format_display_number(float(recipe.get('sugar_g') or 0))} g",
        "Sodium: "
        f"{format_display_number(float(recipe.get('sodium_mg') or 0), decimals=0)} mg",
        "",
        "Ingredients:",
    ])
    for ingredient in recipe.get("ingredients") or []:
        lines.append(
            f"- {ingredient.get('amount') or 'as needed'} "
            f"{ingredient.get('name') or 'ingredient'}"
        )
    lines.extend(["", "Preparation:"])
    for index, step in enumerate(
        recipe.get("preparation_steps") or [],
        start=1,
    ):
        lines.append(f"{index}. {step}")
    if recipe.get("estimate_notes"):
        lines.extend(["", f"Estimate note: {recipe['estimate_notes']}"])
    lines.extend([
        "",
        "This recipe uses estimated nutrition. Nothing has been logged.",
        "",
        "Reply Log Recipe, Edit Recipe, Delete Recipe, Back, or Cancel.",
    ])
    return "\n".join(lines).strip()


def healthcoach_pantry_menu_text() -> str:
    return (
        "My Pantry Menu\n\n"
        "1. View pantry\n"
        "2. Add items manually\n"
        "3. Scan product into Pantry\n"
        "4. Add items from shelf photo\n"
        "5. Organize pantry\n"
        "6. Complete Pantry nutrition\n"
        "7. Get meal ideas\n"
        "8. Smart Pantry swaps\n"
        "9. Shopping list\n"
        "10. Remove pantry item\n"
        "11. Clear pantry\n"
        "12. Back\n\n"
        "Pantry items stay available until you remove or clear "
        "them. Quantities are not tracked."
    )


def healthcoach_shopping_list_menu_text() -> str:
    return (
        "Shopping List Menu\n\n"
        "1. View list\n"
        "2. Add items manually\n"
        "3. Mark purchased\n"
        "4. Remove item\n"
        "5. Clear list\n"
        "6. Back\n\n"
        "Shopping List items stay saved until you remove, clear, or "
        "mark them purchased. Purchased items move to My Pantry."
    )


def pantry_meal_type_prompt() -> str:
    return (
        "What meal do you want Pantry ideas for?\n\n"
        "Lunch ideas stay at or below 500 calories.\n"
        "Dinner ideas stay at or below 600 calories.\n\n"
        "HealthCoach will consider what you have logged today, your "
        "saved calorie goal when available, and linked Pantry nutrition. "
        "It will follow the Pantry-only or extra-ingredient choice you "
        "just made."
    )


def pantry_meal_additional_ingredients_prompt() -> str:
    return (
        "Can these meal ideas include foods that are not currently "
        "in My Pantry?\n\n"
        "1. Pantry only — no shopping\n"
        "2. Up to 2 extra ingredients\n\n"
        "Water, salt, pepper, and cooking spray may be assumed. Pantry "
        "tracks whether an item is present, not how much remains."
    )


def format_pantry_goal_context(
    goal_context: dict | None,
) -> list[str]:
    if not goal_context:
        return []

    low = float(goal_context["saved_target_low"])
    high = float(goal_context["saved_target_high"])
    logged = float(goal_context["logged_calories"])
    lines = [
        "Saved goal context",
        "- Target: "
        f"{format_display_number(low, decimals=0)}-"
        f"{format_display_number(high, decimals=0)} calories/day",
        "- Logged before this meal: "
        f"{format_display_number(logged, decimals=0)} calories",
    ]
    if logged < low:
        lines.append(
            "- Remaining to saved range: "
            f"{format_display_number(low - logged, decimals=0)}-"
            f"{format_display_number(high - logged, decimals=0)} calories"
        )
    elif logged <= high:
        lines.append(
            "- Current logged total is within the saved range."
        )
    else:
        lines.append(
            "- Current logged total is "
            f"{format_display_number(logged - high, decimals=0)} calories "
            "above the saved range."
        )

    calculation_date = goal_context.get("calculation_date")
    if calculation_date:
        lines.append(f"- Goal last updated: {calculation_date}")
    missing = int(goal_context.get("missing_calorie_items") or 0)
    if missing:
        lines.append(
            f"- {missing} logged item(s) have no calories and are excluded."
        )
    return [*lines, ""]


def format_pantry_meal_ideas(
    ideas: list[dict],
    *,
    meal_type: str,
    goal_context: dict | None = None,
) -> str:
    lines = [
        f"Pantry Meal Ideas — {meal_type.title()}",
        "",
        *format_pantry_goal_context(goal_context),
    ]

    for index, idea in enumerate(ideas, start=1):
        ingredients = list(idea.get("ingredients") or [])
        pantry_names = [
            str(item.get("name") or "")
            for item in ingredients
            if item.get("source") == "pantry"
        ]
        additional_names = [
            str(item.get("name") or "")
            for item in ingredients
            if item.get("source") == "additional"
        ]

        heart_label = (
            " — Heart-Healthy Pick"
            if idea.get("heart_healthy_pick")
            else ""
        )

        lines.extend(
            [
                f"{index}. {idea.get('name') or 'Meal idea'}"
                f"{heart_label}",
                (
                    "Estimated: "
                    f"{format_display_number(float(idea.get('calories') or 0), decimals=0)} "
                    "cal, "
                    f"{format_display_number(float(idea.get('protein_g') or 0))} "
                    "g protein"
                ),
                "Uses: " + ", ".join(pantry_names),
                (
                    "Needs: " + ", ".join(additional_names)
                    if additional_names
                    else "Needs: no additional ingredients"
                ),
                f"Nutrition fit: {idea.get('daily_fit') or ''}",
                *(
                    [f"Goal fit: {idea.get('goal_fit')}"]
                    if idea.get("goal_fit")
                    else []
                ),
                *(
                    [
                        "Heart-healthy note: "
                        f"{idea.get('heart_healthy_reason') or ''}"
                    ]
                    if idea.get("heart_healthy_pick")
                    else []
                ),
                "",
            ]
        )

    lines.extend(
        [
            "Choose 1, 2, or 3 for ingredients and preparation.",
            "Reply More ideas for three different choices, Back, "
            "or Cancel.",
            "",
            "Nutrition is estimated. Nothing has been logged.",
            "Heart-Healthy Pick is based on the meal's ingredients "
            "and estimated nutrition. It is not a medical rating.",
        ]
    )
    return "\n".join(lines).strip()


def format_pantry_meal_idea_details(
    idea: dict,
    *,
    meal_type: str,
) -> str:
    lines = [
        "Pantry Meal Idea",
        "",
        str(idea.get("name") or "Meal idea"),
        *(
            [
                "Heart-Healthy Pick",
                "Heart-healthy note: "
                f"{idea.get('heart_healthy_reason') or ''}",
            ]
            if idea.get("heart_healthy_pick")
            else []
        ),
        str(idea.get("summary") or ""),
        "",
        "Estimated nutrition for 1 serving:",
        (
            "Calories: "
            f"{format_display_number(float(idea.get('calories') or 0), decimals=0)}"
        ),
        (
            "Protein: "
            f"{format_display_number(float(idea.get('protein_g') or 0))} g"
        ),
        (
            "Carbohydrates: "
            f"{format_display_number(float(idea.get('carbohydrates_g') or 0))} g"
        ),
        (
            "Fat: "
            f"{format_display_number(float(idea.get('fat_g') or 0))} g"
        ),
        (
            "Fiber: "
            f"{format_display_number(float(idea.get('fiber_g') or 0))} g"
        ),
        (
            "Sugar: "
            f"{format_display_number(float(idea.get('sugar_g') or 0))} g"
        ),
        (
            "Sodium: "
            f"{format_display_number(float(idea.get('sodium_mg') or 0), decimals=0)} mg"
        ),
        "",
        "Ingredients:",
    ]

    for ingredient in idea.get("ingredients") or []:
        source = (
            "Pantry"
            if ingredient.get("source") == "pantry"
            else "additional"
        )
        lines.append(
            f"- {ingredient.get('amount') or 'as needed'} "
            f"{ingredient.get('name') or 'ingredient'} ({source})"
        )

    lines.extend(["", "Preparation:"])
    for index, step in enumerate(
        idea.get("preparation_steps") or [],
        start=1,
    ):
        lines.append(f"{index}. {step}")

    lines.extend(
        [
            "",
            f"Nutrition fit: {idea.get('daily_fit') or ''}",
            *(
                [f"Goal fit: {idea.get('goal_fit')}"]
                if idea.get("goal_fit")
                else []
            ),
            "Nutrition-data basis: "
            f"{idea.get('nutrition_basis') or 'Nutrition is estimated.'}",
            f"Estimate note: {idea.get('estimate_notes') or ''}",
            "",
            f"This is an estimated {meal_type.title()} recipe. "
            "Nothing has been logged.",
            "Heart-Healthy Pick is a food-choice label, not a "
            "medical rating.",
            "",
            "Reply Log Meal, Save Recipe, More ideas, Back, or Cancel.",
        ]
    )
    return "\n".join(lines).strip()


def show_pantry_meal_ideas(
    *,
    chat_id,
    meal_type: str,
    allow_additional_ingredients: bool = True,
) -> bool:
    pantry_items = list_pantry_items()
    if not pantry_items:
        send_telegram_msg(
            "Your Pantry is empty. Add a few available foods or "
            "scan products before requesting meal ideas.",
            chat_id=chat_id,
        )
        return False

    normalized_meal = str(meal_type or "").strip().lower()
    if normalized_meal not in MEAL_CALORIE_LIMITS:
        raise ValueError("meal_type must be lunch or dinner.")

    send_telegram_msg(
        f"I'm building three {normalized_meal} ideas from your "
        "Pantry, today's food log, and saved goal when available. "
        "This may take a moment.",
        chat_id=chat_id,
        remove_keyboard=True,
    )

    try:
        today = datetime.now(PACIFIC_TZ).date()
        daily_totals = get_daily_totals(today)
        entries = list_food_entries(entry_date=today)
        goal_context = build_pantry_goal_context(
            daily_totals=daily_totals,
            saved_goal=get_latest_weight_goal_calculation(),
            missing_calorie_items=sum(
                1 for entry in entries if entry.get("calories") is None
            ),
        )
        ideas = generate_pantry_meal_ideas(
            pantry_items=pantry_items,
            meal_type=normalized_meal,
            daily_totals=daily_totals,
            goal_context=goal_context,
            max_additional_ingredients=(
                MAX_ADDITIONAL_INGREDIENTS
                if allow_additional_ingredients
                else 0
            ),
        )
    except Exception:
        logging.exception("Pantry meal idea generation failed")
        send_telegram_msg(
            "I couldn't build safe Pantry meal ideas right now. "
            "Your Pantry and food log were not changed. Please "
            "try again in a moment.",
            chat_id=chat_id,
        )
        return False

    update_conversation(
        chat_id=chat_id,
        current_step="pantry_meal_ideas",
        known_data={
            "pantry_meal_type": normalized_meal,
            "pantry_meal_ideas": ideas,
            "pantry_meal_selected_index": None,
            "pantry_meal_servings": None,
            "pantry_meal_goal_context": goal_context,
            "pantry_meal_allow_additional": bool(
                allow_additional_ingredients
            ),
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_meal_ideas(
            ideas,
            meal_type=normalized_meal,
            goal_context=goal_context,
        ),
        chat_id=chat_id,
    )
    return True


def format_smart_pantry_swaps(swaps: list[dict]) -> str:
    lines = ["Smart Pantry Swaps", ""]

    if not swaps:
        lines.extend(
            [
                "I didn't find a high-value replacement worth pushing "
                "right now. Your current Pantry choices already look "
                "reasonable based on the information available.",
                "",
            ]
        )
    else:
        for index, swap in enumerate(swaps, start=1):
            basis = (
                "saved package nutrition"
                if swap.get("evidence_basis") == "known_nutrition"
                else "general food-pattern guidance"
            )
            lines.extend(
                [
                    f"{index}. Replace: "
                    f"{swap.get('pantry_item_name') or 'Pantry item'}",
                    "Try: "
                    f"{swap.get('suggested_replacement') or 'alternative'}",
                    f"Why: {swap.get('why_it_helps') or ''}",
                    f"Shopping tip: {swap.get('shopping_tip') or ''}",
                    "Heart-health note: "
                    f"{swap.get('heart_health_note') or ''}",
                    f"Basis: {basis}",
                    *(
                        [
                            "Already in Pantry: "
                            f"{swap.get('available_pantry_item_name')}"
                        ]
                        if swap.get("available_pantry_item_name")
                        else [
                            f"Shopping List: reply Add {index} to save "
                            "this replacement"
                        ]
                    ),
                    "",
                ]
            )

    lines.extend(
        [
            "These are optional shopping suggestions. Nothing in your "
            "Pantry has been changed.",
            "Heart-health notes are general food guidance, not a medical "
            "rating or diagnosis.",
            "",
            "Reply Add 1, Add 2, or Add 3 to save a replacement; "
            "Shopping list to view saved items; Refresh swaps; Back; "
            "or Cancel.",
        ]
    )
    return "\n".join(lines).strip()


def show_smart_pantry_swaps(*, chat_id) -> bool:
    pantry_items = list_pantry_items()
    if not pantry_items:
        send_telegram_msg(
            "Your Pantry is empty. Add a few available foods or scan "
            "products before requesting swaps.",
            chat_id=chat_id,
        )
        return False

    send_telegram_msg(
        "I'm reviewing your Pantry for a few practical, higher-value "
        "replacements. This may take a moment.",
        chat_id=chat_id,
        remove_keyboard=True,
    )

    try:
        swaps = generate_smart_pantry_swaps(
            pantry_items=pantry_items,
        )
    except Exception:
        logging.exception("Smart Pantry swap generation failed")
        send_telegram_msg(
            "I couldn't review Pantry swaps right now. Your Pantry was "
            "not changed. Please try again in a moment.",
            chat_id=chat_id,
        )
        return False

    update_conversation(
        chat_id=chat_id,
        current_step="pantry_swaps",
        known_data={"pantry_swaps": swaps},
        missing_fields=[],
    )
    send_telegram_msg(
        format_smart_pantry_swaps(swaps),
        chat_id=chat_id,
    )
    return True


def format_shopping_list(items: list[dict]) -> str:
    lines = ["Shopping List", ""]
    if not items:
        lines.append("Your Shopping List is empty.")
    else:
        for index, item in enumerate(items, start=1):
            line = f"{index}. {item['display_name']}"
            if item.get("source_note"):
                line += f" — {item['source_note']}"
            lines.append(line)

    lines.extend(
        [
            "",
            "Items stay saved until removed, cleared, or marked "
            "purchased.",
            "Reply Back to return or Cancel to close.",
        ]
    )
    return "\n".join(lines).strip()


def format_shopping_item_choices(
    items: list[dict],
    *,
    action: str,
) -> str:
    verb = "mark purchased" if action == "purchase" else "remove"
    lines = [f"Choose a Shopping List item to {verb}:", ""]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item['display_name']}")
    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


PANTRY_PAGE_SIZE = 12
PANTRY_NUTRITION_PAGE_SIZE = 10
PANTRY_SAVED_FOOD_PAGE_SIZE = 10


def pantry_page(
    items: list[dict],
    *,
    page: int = 0,
) -> tuple[list[dict], int, int]:
    """Return one stable storage-grouped Pantry page."""
    storage_order = {
        key: index
        for index, key in enumerate(
            (
                "pantry_shelf",
                "refrigerator",
                "freezer",
                "counter_produce",
                "other",
                "unsorted",
            )
        )
    }
    ordered = sorted(
        items,
        key=lambda item: (
            storage_order.get(str(item.get("storage_area")), 999),
            str(item.get("display_name") or "").lower(),
            int(item.get("pantry_item_id") or 0),
        ),
    )
    total_pages = max(
        1,
        (len(ordered) + PANTRY_PAGE_SIZE - 1) // PANTRY_PAGE_SIZE,
    )
    safe_page = max(0, min(int(page), total_pages - 1))
    start = safe_page * PANTRY_PAGE_SIZE
    return (
        ordered[start : start + PANTRY_PAGE_SIZE],
        safe_page,
        total_pages,
    )


def pantry_nutrition_ready(item: dict) -> bool:
    """Return whether one Pantry item has linked usable nutrition."""
    return (
        item.get("nutrition_version_id") is not None
        and item.get("calories") is not None
    )


def pantry_needs_nutrition(item: dict) -> bool:
    """Return whether one Pantry item still needs a trusted Food link."""
    return not pantry_nutrition_ready(item)


def simple_page(
    items: list[dict],
    *,
    page: int,
    page_size: int,
) -> tuple[list[dict], int, int]:
    """Return one clamped page without changing item order."""
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    safe_page = max(0, min(int(page), total_pages - 1))
    start = safe_page * page_size
    return items[start : start + page_size], safe_page, total_pages


def pantry_nutrition_items(
    items: list[dict],
    *,
    skipped_ids: list[int] | None = None,
) -> list[dict]:
    """Return stable nutrition-incomplete items not skipped this session."""
    skipped = {int(value) for value in (skipped_ids or [])}
    return sorted(
        (
            item
            for item in items
            if pantry_needs_nutrition(item)
            and int(item.get("pantry_item_id") or 0) not in skipped
        ),
        key=lambda item: (
            str(item.get("display_name") or "").lower(),
            int(item.get("pantry_item_id") or 0),
        ),
    )


def pantry_choice_key(
    value: str,
    choices: dict[str, str],
    *,
    aliases: dict[str, str] | None = None,
) -> str | None:
    """Resolve a numbered or labeled Pantry organization choice."""
    lowered = str(value or "").strip().lower()
    try:
        selected_index = int(lowered) - 1
    except ValueError:
        selected_index = -1
    keys = list(choices)
    if 0 <= selected_index < len(keys):
        return keys[selected_index]
    for key, label in choices.items():
        if lowered in {key.lower(), label.lower()}:
            return key
    return (aliases or {}).get(lowered)


def format_pantry_items(items: list[dict], *, page: int = 0) -> str:
    if not items:
        return (
            "My Pantry\n\n"
            "Your Pantry is empty.\n\n"
            "Reply Back to return or Cancel to close."
        )

    page_items, safe_page, total_pages = pantry_page(items, page=page)
    nutrition_ready_count = sum(
        1 for item in items if pantry_nutrition_ready(item)
    )
    lines = [
        f"My Pantry — Page {safe_page + 1} of {total_pages}",
        "",
    ]
    previous_storage = None
    for index, item in enumerate(page_items, start=1):
        storage_area = str(item.get("storage_area") or "unsorted")
        if storage_area != previous_storage:
            if previous_storage is not None:
                lines.append("")
            lines.append(
                str(
                    PANTRY_STORAGE_AREAS.get(
                        storage_area,
                        "Unsorted",
                    )
                ).upper()
            )
            previous_storage = storage_area
        name = str(item.get("display_name") or "Pantry item")
        category = PANTRY_FOOD_CATEGORIES.get(
            str(item.get("food_category") or "unsorted"),
            "Unsorted",
        )
        nutrition_status = (
            "nutrition ready"
            if pantry_nutrition_ready(item)
            else "needs nutrition"
        )
        lines.append(
            f"{index}. {name} — {category} — {nutrition_status}"
        )

    lines.extend([
        "",
        f"Nutrition ready: {nutrition_ready_count}/{len(items)} items",
        "These items are remembered as available; quantities are not tracked.",
        "",
        "Reply Next, Previous, Back, or Cancel.",
    ])
    return "\n".join(lines)


def format_pantry_photo_result(
    detected_names: list[str],
    pending_names: list[str],
) -> str:
    """Show what one photo added to the pending, unsaved review."""
    lines = ["Pantry Shelf Photo — Items Found", ""]
    if detected_names:
        lines.extend(["Clearly visible in this photo:"])
        lines.extend(f"- {name}" for name in detected_names)
    else:
        lines.append(
            "No new distinct items were added from this photo. They may "
            "already be in the pending review."
        )
    lines.extend([
        "",
        f"Pending review: {len(pending_names)} item(s)",
        "Nothing has been added to My Pantry yet.",
        "",
        "Reply Add another photo, Review items, or Cancel.",
    ])
    return "\n".join(lines)


def format_pantry_photo_review(names: list[str]) -> str:
    """Show the complete deduplicated shelf-photo list before insertion."""
    lines = [
        "Pantry Shelf Photo — Review",
        "",
        "These visible items are ready to add:",
        "",
    ]
    lines.extend(
        f"{index}. {name}"
        for index, name in enumerate(names, start=1)
    )
    lines.extend([
        "",
        "Duplicates already in My Pantry will not be added twice.",
        "Photo names are presence-only and do not create nutrition data.",
        "Nothing has been added yet.",
        "",
        "Reply Add all to Pantry, Edit item name, Remove an item, Add "
        "another photo, or Cancel.",
    ])
    return "\n".join(lines)


def format_pantry_photo_remove_choices(names: list[str]) -> str:
    """Show pending photo items that may be removed before confirmation."""
    lines = [
        "Pantry Shelf Photo — Remove Item",
        "",
        "Choose an item to remove from this pending review:",
        "",
    ]
    lines.extend(
        f"{index}. {name}"
        for index, name in enumerate(names, start=1)
    )
    lines.extend(["", "Reply Back to return without removing anything."])
    return "\n".join(lines)


def format_pantry_photo_edit_choices(names: list[str]) -> str:
    """Show pending photo items whose display name may be corrected."""
    lines = [
        "Pantry Shelf Photo — Edit Item",
        "",
        "Choose an item whose name should be corrected:",
        "",
    ]
    lines.extend(
        f"{index}. {name}"
        for index, name in enumerate(names, start=1)
    )
    lines.extend(["", "Reply Back to return without changing a name."])
    return "\n".join(lines)


def format_pantry_organize_choices(
    items: list[dict],
    *,
    page: int = 0,
) -> str:
    page_items, safe_page, total_pages = pantry_page(items, page=page)
    lines = [
        "Pantry Organizer — Choose Item",
        f"Page {safe_page + 1} of {total_pages}",
        "",
    ]
    for index, item in enumerate(page_items, start=1):
        storage = PANTRY_STORAGE_AREAS.get(
            str(item.get("storage_area") or "unsorted"),
            "Unsorted",
        )
        category = PANTRY_FOOD_CATEGORIES.get(
            str(item.get("food_category") or "unsorted"),
            "Unsorted",
        )
        lines.append(
            f"{index}. {item.get('display_name') or 'Pantry item'} "
            f"— {storage}; {category}"
        )
    lines.extend([
        "",
        "Choose a number, Search, Next, Previous, or Back.",
    ])
    return "\n".join(lines)


def pantry_organizer_search_items(
    items: list[dict],
    query: str,
) -> list[dict]:
    """Return Pantry items matching a partial personal or linked name."""
    normalized_query = " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(query or "").casefold()).split()
    )
    if not normalized_query:
        return []
    query_tokens = normalized_query.split()
    matches = []
    for item in items:
        searchable = " ".join(
            str(item.get(field) or "")
            for field in ("display_name", "canonical_name", "brand")
        )
        normalized_searchable = " ".join(
            re.sub(
                r"[^a-z0-9]+",
                " ",
                searchable.casefold(),
            ).split()
        )
        if (
            normalized_query in normalized_searchable
            or all(token in normalized_searchable for token in query_tokens)
        ):
            matches.append(item)
    return sorted(
        matches,
        key=lambda item: (
            str(item.get("display_name") or "").casefold(),
            int(item.get("pantry_item_id") or 0),
        ),
    )


def format_pantry_organizer_search_results(
    items: list[dict],
    *,
    query: str,
    page: int = 0,
) -> str:
    """Show one review-only page of Pantry organizer search matches."""
    page_items, safe_page, total_pages = simple_page(
        items,
        page=page,
        page_size=PANTRY_PAGE_SIZE,
    )
    lines = [
        "Pantry Organizer — Search Results",
        f"Search: {query}",
        f"Page {safe_page + 1} of {total_pages}",
        "",
    ]
    for index, item in enumerate(page_items, start=1):
        storage = PANTRY_STORAGE_AREAS.get(
            str(item.get("storage_area") or "unsorted"),
            "Unsorted",
        )
        category = PANTRY_FOOD_CATEGORIES.get(
            str(item.get("food_category") or "unsorted"),
            "Unsorted",
        )
        lines.append(
            f"{index}. {item.get('display_name') or 'Pantry item'} "
            f"— {storage}; {category}"
        )
    lines.extend([
        "",
        f"Matches: {len(items)}",
        "Choose a number, Search again, Next, Previous, or Back.",
    ])
    return "\n".join(lines)


def format_pantry_organize_action(item: dict) -> str:
    """Show the editable fields for one selected Pantry item."""
    storage = PANTRY_STORAGE_AREAS.get(
        str(item.get("storage_area") or "unsorted"),
        "Unsorted",
    )
    category = PANTRY_FOOD_CATEGORIES.get(
        str(item.get("food_category") or "unsorted"),
        "Unsorted",
    )
    return (
        "Pantry Organizer — Item\n\n"
        f"Item: {item.get('display_name') or 'Pantry item'}\n"
        f"Storage: {storage}\n"
        f"Food type: {category}\n\n"
        "1. Change name\n"
        "2. Change storage and food type\n"
        "3. View or change nutrition\n"
        "4. Delete Pantry item\n"
        "5. Back"
    )


def format_pantry_organize_nutrition(item: dict) -> str:
    """Show linked nutrition and safe update choices for one Pantry item."""
    if not pantry_nutrition_ready(item):
        return (
            "Pantry Organizer — Nutrition\n\n"
            f"Item: {item.get('display_name') or 'Pantry item'}\n"
            "Nutrition status: not linked\n\n"
            "1. Add or link nutrition\n"
            "2. Back\n\n"
            "Nothing will be saved without confirmation."
        )

    def nutrient(field: str, suffix: str) -> str:
        value = item.get(field)
        return (
            "not available"
            if value is None
            else f"{format_display_number(float(value))} {suffix}".strip()
        )

    return (
        "Pantry Organizer — Nutrition\n\n"
        f"Item: {item.get('display_name') or 'Pantry item'}\n"
        f"Linked Food: {item.get('canonical_name') or 'Saved Food'}\n"
        f"Serving: {item.get('serving_description') or '1 serving'}\n"
        f"Version: {int(item.get('version_number') or 1)}\n"
        f"Source: {item.get('verification_source') or 'saved source'}\n\n"
        f"Calories: {nutrient('calories', 'cal')}\n"
        f"Protein: {nutrient('protein_g', 'g')}\n"
        f"Carbohydrates: {nutrient('carbohydrates_g', 'g')}\n"
        f"Fat: {nutrient('fat_g', 'g')}\n"
        f"Fiber: {nutrient('fiber_g', 'g')}\n"
        f"Sugar: {nutrient('sugar_g', 'g')}\n"
        f"Sodium: {nutrient('sodium_mg', 'mg')}\n\n"
        "1. Correct current values\n"
        "2. Replace linked nutrition\n"
        "3. Remove nutrition link\n"
        "4. Back\n\n"
        "Corrections create a new version for future uses. Previous logs "
        "and saved recipe calculations remain unchanged."
    )


def format_pantry_nutrition_choices(
    items: list[dict],
    *,
    page: int = 0,
    total_incomplete: int | None = None,
) -> str:
    """Show one page of Pantry items that need nutrition."""
    page_items, safe_page, total_pages = simple_page(
        items,
        page=page,
        page_size=PANTRY_NUTRITION_PAGE_SIZE,
    )
    lines = [
        "Complete Pantry Nutrition — Choose Item",
        f"Page {safe_page + 1} of {total_pages}",
        "",
    ]
    lines.extend(
        f"{index}. {item.get('display_name') or 'Pantry item'}"
        for index, item in enumerate(page_items, start=1)
    )
    lines.extend([
        "",
        (
            f"Needs nutrition: {int(total_incomplete)} item(s)"
            if total_incomplete is not None
            else f"Needs nutrition: {len(items)} item(s)"
        ),
        "Choose a number, Next, Previous, Back, or Cancel.",
    ])
    return "\n".join(lines)


def format_pantry_nutrition_options(
    item: dict,
    *,
    organizer_mode: bool = False,
) -> str:
    """Show trusted completion choices for one Pantry item."""
    lines = (
        "Complete Pantry Nutrition — Options\n\n"
        f"Item: {item.get('display_name') or 'Pantry item'}\n\n"
        "1. Link existing nutrition\n"
        "2. Try verified lookup\n"
        "3. Scan barcode\n"
        "4. Photograph label\n"
        "5. Enter manually\n"
    )
    if organizer_mode:
        lines += "6. Back\n\n"
    else:
        lines += "6. Skip\n7. Back\n\n"
    return lines + "Nothing will be linked or saved without confirmation."


def format_pantry_saved_food_choices(
    foods: list[dict],
    *,
    pantry_name: str,
    page: int = 0,
) -> str:
    """Show one page of trusted existing nutrition to link."""
    page_foods, safe_page, total_pages = simple_page(
        foods,
        page=page,
        page_size=PANTRY_SAVED_FOOD_PAGE_SIZE,
    )
    lines = [
        "Complete Pantry Nutrition — Existing Nutrition",
        f"Pantry item: {pantry_name}",
        f"Page {safe_page + 1} of {total_pages}",
        "",
        "Only close name matches are shown. Review the product, serving, "
        "and source before linking.",
        "",
    ]
    for index, food in enumerate(page_foods, start=1):
        lines.append(
            f"{index}. {food.get('canonical_name') or 'Saved Food'} — "
            f"{food.get('serving_description') or '1 serving'} — "
            f"{format_display_number(float(food.get('calories') or 0), decimals=0)} cal — "
            f"{food.get('verification_source') or 'saved source'}"
        )
    lines.extend([
        "",
        "Choose a number, Next, Previous, Back, or Cancel.",
    ])
    return "\n".join(lines)


def format_pantry_nutrition_review(
    *,
    heading: str,
    pantry_name: str,
    food: dict,
    nutrition: dict,
    source: str,
) -> str:
    """Format one explicit Pantry nutrition-link confirmation."""
    def nutrient(field: str, suffix: str) -> str:
        value = nutrition.get(field)
        return (
            "not available"
            if value is None
            else f"{format_display_number(float(value))} {suffix}".strip()
        )

    return (
        f"{heading}\n\n"
        f"Pantry item: {pantry_name}\n"
        f"Food: {food.get('canonical_name') or pantry_name}\n"
        "Serving: "
        f"{food.get('serving_description') or '1 serving'}\n"
        f"Source: {source}\n\n"
        f"Calories: {nutrient('calories', 'cal')}\n"
        f"Protein: {nutrient('protein_g', 'g')}\n"
        f"Carbohydrates: {nutrient('carbohydrates_g', 'g')}\n"
        f"Fat: {nutrient('fat_g', 'g')}\n"
        f"Fiber: {nutrient('fiber_g', 'g')}\n"
        f"Sugar: {nutrient('sugar_g', 'g')}\n"
        f"Sodium: {nutrient('sodium_mg', 'mg')}\n\n"
        "Nothing has been linked or saved yet.\n\n"
        "1. Yes\n"
        "2. No"
    )


def format_pantry_storage_choices(item_name: str) -> str:
    lines = [
        "Pantry Organizer — Storage Area",
        "",
        f"Item: {item_name}",
        "",
    ]
    lines.extend(
        f"{index}. {label}"
        for index, label in enumerate(PANTRY_STORAGE_AREAS.values(), start=1)
    )
    lines.extend(["", "Reply Back to choose a different item."])
    return "\n".join(lines)


def format_pantry_category_choices(item_name: str) -> str:
    lines = [
        "Pantry Organizer — Food Type",
        "",
        f"Item: {item_name}",
        "",
    ]
    lines.extend(
        f"{index}. {label}"
        for index, label in enumerate(PANTRY_FOOD_CATEGORIES.values(), start=1)
    )
    lines.extend(["", "Reply Back to change the storage area."])
    return "\n".join(lines)


def format_pantry_remove_choices(
    items: list[dict],
    *,
    page: int = 0,
) -> str:
    page_items, safe_page, total_pages = pantry_page(items, page=page)
    lines = [
        "Choose a Pantry item to remove:",
        f"Page {safe_page + 1} of {total_pages}",
        "",
    ]
    for index, item in enumerate(page_items, start=1):
        lines.append(
            f"{index}. {item.get('display_name') or 'Pantry item'}"
        )
    lines.extend(["", "Reply Next, Previous, or Back."])
    return "\n".join(lines)


def show_pantry_view_page(
    *,
    chat_id: int | str,
    page: int = 0,
) -> None:
    items = list_pantry_items()
    _, safe_page, _ = pantry_page(items, page=page)
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_view",
        known_data={"pantry_page": safe_page},
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_items(items, page=safe_page),
        chat_id=chat_id,
    )


def show_pantry_organizer_page(
    *,
    chat_id: int | str,
    page: int = 0,
) -> bool:
    items = list_pantry_items()
    if not items:
        send_telegram_msg(
            "Your Pantry is empty. Add some items before organizing it.",
            chat_id=chat_id,
        )
        return False
    page_items, safe_page, _ = pantry_page(items, page=page)
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_organize_select",
        known_data={
            "pantry_page": safe_page,
            "pantry_item_ids": [
                int(item["pantry_item_id"])
                for item in page_items
            ],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_organize_choices(items, page=safe_page),
        chat_id=chat_id,
    )
    return True


def show_pantry_organizer_search_results(
    *,
    chat_id: int | str,
    query: str,
    page: int = 0,
) -> bool:
    """Search current Pantry items and show one selectable result page."""
    matches = pantry_organizer_search_items(list_pantry_items(), query)
    if not matches:
        update_conversation(
            chat_id=chat_id,
            current_step="pantry_organize_search_query",
            known_data={"pantry_page": 0},
            missing_fields=["pantry_search_query"],
        )
        send_telegram_msg(
            "No Pantry items matched that search.\n\n"
            "Try part of the Pantry name, linked Food name, or brand. "
            "Reply Back to return.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return False
    page_items, safe_page, _ = simple_page(
        matches,
        page=page,
        page_size=PANTRY_PAGE_SIZE,
    )
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_organize_search_results",
        known_data={
            "pantry_search_query": str(query).strip(),
            "pantry_search_page": safe_page,
            "pantry_search_item_ids": [
                int(item["pantry_item_id"])
                for item in page_items
            ],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_organizer_search_results(
            matches,
            query=str(query).strip(),
            page=safe_page,
        ),
        chat_id=chat_id,
    )
    return True


def get_pantry_item_by_id(pantry_item_id: int) -> dict | None:
    """Return one current Pantry item from the joined Pantry view."""
    return next(
        (
            item
            for item in list_pantry_items()
            if int(item["pantry_item_id"]) == int(pantry_item_id)
        ),
        None,
    )


def show_pantry_nutrition_queue(
    *,
    chat_id: int | str,
    page: int = 0,
    skipped_ids: list[int] | None = None,
) -> bool:
    """Show one page of Pantry items needing trusted nutrition."""
    all_items = list_pantry_items()
    all_incomplete = [
        item for item in all_items if pantry_needs_nutrition(item)
    ]
    remaining = pantry_nutrition_items(
        all_items,
        skipped_ids=skipped_ids,
    )
    if not remaining:
        update_conversation(
            chat_id=chat_id,
            current_step="pantry",
            known_data={
                "pantry_nutrition_skipped_ids": [],
                "pantry_nutrition_item_id": None,
            },
            missing_fields=[],
        )
        if all_incomplete:
            message = (
                "No more unskipped Pantry items remain in this session.\n\n"
                f"{len(all_incomplete)} item(s) still need nutrition. "
                "Choose Complete Pantry nutrition when you want to review "
                "them again."
            )
        else:
            message = (
                "Pantry nutrition is complete. Every Pantry item has linked "
                "nutrition."
            )
        send_telegram_msg(
            message + "\n\n" + healthcoach_pantry_menu_text(),
            chat_id=chat_id,
        )
        return False

    page_items, safe_page, _ = simple_page(
        remaining,
        page=page,
        page_size=PANTRY_NUTRITION_PAGE_SIZE,
    )
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_nutrition_select",
        known_data={
            "pantry_nutrition_page": safe_page,
            "pantry_nutrition_skipped_ids": list(skipped_ids or []),
            "pantry_nutrition_item_ids": [
                int(item["pantry_item_id"])
                for item in page_items
            ],
            "pantry_nutrition_item_id": None,
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_nutrition_choices(
            remaining,
            page=safe_page,
            total_incomplete=len(all_incomplete),
        ),
        chat_id=chat_id,
    )
    return True


def show_pantry_nutrition_options(
    *,
    chat_id: int | str,
    pantry_item_id: int,
    known_data: dict,
) -> bool:
    """Show completion methods for one current Pantry item."""
    organizer_mode = bool(
        known_data.get("_pantry_organizer_nutrition_return")
    )
    item = get_pantry_item_by_id(pantry_item_id)
    if item is None:
        send_telegram_msg(
            "That Pantry item is no longer available.",
            chat_id=chat_id,
        )
        if organizer_mode:
            show_pantry_organizer_page(
                chat_id=chat_id,
                page=int(known_data.get("pantry_page") or 0),
            )
            return False
        show_pantry_nutrition_queue(
            chat_id=chat_id,
            page=int(known_data.get("pantry_nutrition_page") or 0),
            skipped_ids=list(
                known_data.get("pantry_nutrition_skipped_ids") or []
            ),
        )
        return False
    if pantry_nutrition_ready(item) and not organizer_mode:
        send_telegram_msg(
            "That Pantry item already has nutrition linked.",
            chat_id=chat_id,
        )
        show_pantry_nutrition_queue(
            chat_id=chat_id,
            page=int(known_data.get("pantry_nutrition_page") or 0),
            skipped_ids=list(
                known_data.get("pantry_nutrition_skipped_ids") or []
            ),
        )
        return False
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_nutrition_options",
        known_data={
            **known_data,
            "pantry_nutrition_item_id": int(pantry_item_id),
            "pantry_nutrition_item_name": item["display_name"],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_nutrition_options(
            item,
            organizer_mode=organizer_mode,
        ),
        chat_id=chat_id,
    )
    return True


def pantry_linkable_foods(pantry_name: str) -> list[dict]:
    """Return only trusted nutrition Foods with a close name match."""
    requested_tokens = normalized_food_tokens(pantry_name)

    # A shared color, preparation word, or broad food family is not enough
    # to suggest that two Pantry names describe the same food. The chooser is
    # deliberately conservative because verified lookup and manual label
    # entry remain available when no close existing record is found.
    weak_shared_tokens = {
        "black",
        "blue",
        "brown",
        "cheese",
        "crumbled",
        "crumble",
        "diced",
        "extra",
        "fresh",
        "frozen",
        "green",
        "large",
        "light",
        "medium",
        "original",
        "red",
        "rice",
        "shredded",
        "sliced",
        "small",
        "white",
        "yellow",
    }
    product_form_tokens = {
        "broth",
        "butter",
        "cream",
        "dressing",
        "flour",
        "juice",
        "milk",
        "oil",
        "powder",
        "sauce",
        "soup",
        "yogurt",
    }

    def candidate_tokens(food: dict) -> set[str]:
        return normalized_food_tokens(
            " ".join(
                str(value)
                for value in (
                    food.get("canonical_name"),
                    food.get("brand"),
                )
                if value
            )
        )

    def is_close_match(food: dict) -> bool:
        tokens = candidate_tokens(food)
        if not requested_tokens or not tokens:
            return False

        # Do not suggest a different product form merely because its source
        # ingredient has the same name, such as olives versus olive oil.
        if (
            requested_tokens & product_form_tokens
        ) != (tokens & product_form_tokens):
            return False

        if requested_tokens == tokens:
            return True

        if requested_tokens.issubset(tokens):
            return True

        if tokens.issubset(requested_tokens):
            return len(tokens) >= 2 or not (
                tokens & weak_shared_tokens
            )

        overlap = requested_tokens & tokens
        meaningful_overlap = overlap - weak_shared_tokens
        return len(overlap) >= 2 or bool(meaningful_overlap)

    def match_rank(food: dict) -> tuple:
        tokens = candidate_tokens(food)
        overlap = len(requested_tokens & tokens)
        exact = bool(requested_tokens) and requested_tokens == tokens
        contained = bool(requested_tokens) and (
            requested_tokens.issubset(tokens)
            or tokens.issubset(requested_tokens)
        )
        return (
            0 if exact else 1 if contained else 2,
            -overlap,
            str(food.get("canonical_name") or "").lower(),
            int(food.get("food_id") or 0),
        )

    return sorted(
        (
            food
            for food in list_nutrition_ready_foods()
            if is_trusted_saved_food(food)
            and food.get("restaurant") is None
            and str(food.get("food_type") or "food") in {"food", "drink"}
            and is_close_match(food)
        ),
        key=match_rank,
    )


def show_pantry_saved_food_page(
    *,
    chat_id: int | str,
    known_data: dict,
    page: int = 0,
) -> bool:
    """Show trusted existing nutrition for one Pantry item."""
    foods = pantry_linkable_foods(
        str(
            known_data.get("pantry_nutrition_item_name")
            or "Pantry item"
        )
    )
    if not foods:
        send_telegram_msg(
            "I couldn't find a close existing nutrition match for this "
            "Pantry item. Unrelated Foods were not shown.\n\n"
            "Try verified lookup, scan the barcode, photograph the label, "
            "or enter the nutrition manually.",
            chat_id=chat_id,
        )
        show_pantry_nutrition_options(
            chat_id=chat_id,
            pantry_item_id=int(
                known_data.get("pantry_nutrition_item_id") or 0
            ),
            known_data=known_data,
        )
        return False
    page_foods, safe_page, _ = simple_page(
        foods,
        page=page,
        page_size=PANTRY_SAVED_FOOD_PAGE_SIZE,
    )
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_nutrition_saved_food_select",
        known_data={
            **known_data,
            "pantry_nutrition_saved_food_page": safe_page,
            "pantry_nutrition_saved_food_ids": [
                int(food["food_id"]) for food in page_foods
            ],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_saved_food_choices(
            foods,
            pantry_name=str(
                known_data.get("pantry_nutrition_item_name")
                or "Pantry item"
            ),
            page=safe_page,
        ),
        chat_id=chat_id,
    )
    return True


def finish_pantry_nutrition_link(
    *,
    chat_id: int | str,
    known_data: dict,
    pantry_item: dict,
    food_name: str,
) -> None:
    """Report one completed link and continue the guided queue."""
    send_telegram_msg(
        "Pantry nutrition linked.\n\n"
        f"Pantry item: {pantry_item.get('display_name')}\n"
        f"Nutrition source food: {food_name}\n\n"
        "Nothing was logged as eaten.",
        chat_id=chat_id,
    )
    if known_data.get("_pantry_organizer_nutrition_return"):
        show_pantry_organize_nutrition(
            chat_id=chat_id,
            pantry_item_id=int(pantry_item["pantry_item_id"]),
            page=int(known_data.get("pantry_page") or 0),
        )
        return
    show_pantry_nutrition_queue(
        chat_id=chat_id,
        page=int(known_data.get("pantry_nutrition_page") or 0),
        skipped_ids=list(
            known_data.get("pantry_nutrition_skipped_ids") or []
        ),
    )


def show_pantry_organize_action(
    *,
    chat_id: int | str,
    pantry_item_id: int,
    page: int = 0,
    search_query: str | None = None,
    search_page: int = 0,
) -> bool:
    """Show the editable actions for one Pantry item."""
    selected = next(
        (
            item
            for item in list_pantry_items()
            if int(item["pantry_item_id"]) == int(pantry_item_id)
        ),
        None,
    )
    if selected is None:
        send_telegram_msg(
            "That Pantry item is no longer available.",
            chat_id=chat_id,
        )
        show_pantry_organizer_page(chat_id=chat_id, page=page)
        return False
    action_data = {
        "pantry_page": int(page),
        "pantry_organize_id": int(pantry_item_id),
        "pantry_organize_name": selected["display_name"],
    }
    if search_query:
        action_data.update({
            "pantry_search_query": str(search_query),
            "pantry_search_page": int(search_page),
            "_pantry_organizer_search_return": True,
        })
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_organize_action",
        known_data=action_data,
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_organize_action(selected),
        chat_id=chat_id,
    )
    return True


def show_pantry_organize_nutrition(
    *,
    chat_id: int | str,
    pantry_item_id: int,
    page: int = 0,
) -> bool:
    """Show current nutrition and safe update actions for a Pantry item."""
    item = get_pantry_item_by_id(pantry_item_id)
    if item is None:
        send_telegram_msg(
            "That Pantry item is no longer available.",
            chat_id=chat_id,
        )
        show_pantry_organizer_page(chat_id=chat_id, page=page)
        return False
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_organize_nutrition",
        known_data={
            "pantry_page": int(page),
            "pantry_organize_id": int(pantry_item_id),
            "pantry_organize_name": item["display_name"],
            "_pantry_organizer_nutrition_return": True,
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_organize_nutrition(item),
        chat_id=chat_id,
    )
    return True


def show_pantry_remove_page(
    *,
    chat_id: int | str,
    page: int = 0,
) -> bool:
    items = list_pantry_items()
    if not items:
        send_telegram_msg(
            "Your Pantry is empty. Nothing can be removed.",
            chat_id=chat_id,
        )
        return False
    page_items, safe_page, _ = pantry_page(items, page=page)
    update_conversation(
        chat_id=chat_id,
        current_step="pantry_remove_select",
        known_data={
            "pantry_page": safe_page,
            "pantry_item_ids": [
                int(item["pantry_item_id"])
                for item in page_items
            ],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_pantry_remove_choices(items, page=safe_page),
        chat_id=chat_id,
    )
    return True


def food_finder_location_labels(food: dict) -> list[str]:
    """Return short, user-facing labels for one Food record."""
    labels = []
    if int(food.get("pantry_count") or 0):
        labels.append("Pantry")
    if bool(food.get("is_saved_recipe")):
        labels.append("Recipe")
    if int(food.get("recipe_ingredient_count") or 0):
        labels.append("Recipe ingredient")
    if int(food.get("favorite_count") or 0):
        labels.append("Favorite")
    if int(food.get("barcode_count") or 0):
        labels.append("Barcode")
    if bool(food.get("is_entered_food")):
        labels.append("Entered food")
    if int(food.get("log_count") or 0):
        labels.append("Logged before")
    return labels or ["Nutrition record only"]


def format_food_finder_results(
    foods: list[dict],
    *,
    query: str,
) -> str:
    """Show concise search results with their current locations."""
    lines = [
        "Food Finder — Results",
        f"Search: {query}",
        "",
    ]
    for index, food in enumerate(foods, start=1):
        brand = str(food.get("brand") or "").strip()
        brand_label = f" — {brand}" if brand else ""
        labels = ", ".join(food_finder_location_labels(food))
        lines.append(
            f"{index}. {food.get('canonical_name') or 'Food'}"
            f"{brand_label} — {labels}"
        )
    lines.extend([
        "",
        "Choose a number to see exactly where the food is stored and used.",
        "Reply Search again, Back, or Cancel.",
    ])
    return "\n".join(lines)


def format_food_finder_details(food: dict) -> str:
    """Explain one Food record without changing its classification."""
    def nutrient(field: str, suffix: str) -> str:
        value = food.get(field)
        if value is None:
            return "not available"
        return f"{format_display_number(float(value))} {suffix}".strip()

    pantry_count = int(food.get("pantry_count") or 0)
    pantry_text = (
        f"Yes — {food.get('pantry_locations')}"
        if pantry_count
        else "No"
    )
    log_count = int(food.get("log_count") or 0)
    log_text = "No"
    if log_count:
        log_word = "entry" if log_count == 1 else "entries"
        log_text = (
            f"{log_count} {log_word}; last logged "
            f"{food.get('last_logged_date') or 'date unavailable'}"
        )
    source = str(food.get("verification_source") or "unknown source")
    brand = str(food.get("brand") or "").strip()
    restaurant = str(food.get("restaurant") or "").strip()
    identity_lines = [
        f"Food: {food.get('canonical_name') or 'Food'}",
    ]
    if brand:
        identity_lines.append(f"Brand: {brand}")
    if restaurant:
        identity_lines.append(f"Restaurant: {restaurant}")
    identity_lines.extend([
        f"Type: {str(food.get('food_type') or 'food').title()}",
        f"Serving: {food.get('serving_description') or 'not available'}",
        f"Nutrition source: {source}",
        "",
        "Nutrition",
        f"- Calories: {nutrient('calories', 'cal')}",
        f"- Protein: {nutrient('protein_g', 'g')}",
        f"- Carbohydrates: {nutrient('carbohydrates_g', 'g')}",
        f"- Fat: {nutrient('fat_g', 'g')}",
        f"- Fiber: {nutrient('fiber_g', 'g')}",
        f"- Sugar: {nutrient('sugar_g', 'g')}",
        f"- Sodium: {nutrient('sodium_mg', 'mg')}",
        "",
        "Where it is",
        "- Food Library: Yes — reusable nutrition record",
        "- Entered Foods list: "
        + ("Yes" if food.get("is_entered_food") else "No"),
        f"- My Pantry: {pantry_text}",
        "- Saved Recipes: "
        + ("Yes" if food.get("is_saved_recipe") else "No"),
        "- Used in Saved Recipes: "
        + (
            f"Yes — {int(food.get('recipe_ingredient_count') or 0)} "
            "ingredient link(s)"
            if int(food.get("recipe_ingredient_count") or 0)
            else "No"
        ),
        "- Favorites: "
        + ("Yes" if int(food.get("favorite_count") or 0) else "No"),
        "- Barcode mapping: "
        + (
            f"Yes — {int(food.get('barcode_count') or 0)} saved"
            if int(food.get("barcode_count") or 0)
            else "No"
        ),
        f"- Food history: {log_text}",
        "",
        "A Food Library record provides reusable nutrition. It does not "
        "mean the food is currently in My Pantry or is a Saved Recipe.",
        "",
        "Reply Manage Food, Search again, Back, or Cancel.",
    ])
    return "Food Finder — Details\n\n" + "\n".join(identity_lines)


def finder_pantry_items(food_id: int) -> list[dict]:
    """Return Pantry placements linked to one Food record."""
    return [
        item
        for item in list_pantry_items()
        if int(item.get("food_id") or 0) == int(food_id)
    ]


def format_food_library_manage(food: dict) -> str:
    """Show safe management actions for one Food Library record."""
    pantry_items = finder_pantry_items(int(food.get("food_id") or 0))
    name_action = (
        "Change name"
        if food.get("is_entered_food")
        else "Add a personal search name"
    )
    pantry_action = (
        "Manage Pantry location and type"
        if pantry_items
        else "Add to Pantry"
    )
    remove_action = (
        "Remove or archive"
        if food.get("is_entered_food") or pantry_items
        else "Review removal options"
    )
    return (
        "Food Library — Manage Food\n\n"
        f"Food: {food.get('canonical_name') or 'Food'}\n"
        f"Source: {food.get('verification_source') or 'unknown source'}\n\n"
        f"1. {name_action}\n"
        f"2. {pantry_action}\n"
        "3. Change nutrition\n"
        f"4. {remove_action}\n"
        "5. Back\n\n"
        "Name and nutrition changes affect future use only. Past logs and "
        "saved recipe calculations stay unchanged."
    )


def show_food_library_manage(
    *,
    chat_id: int | str,
    food_id: int,
    return_data: dict | None = None,
) -> bool:
    """Refresh and show one Food Library management page."""
    food = get_food_location(food_id)
    if food is None:
        send_telegram_msg(
            "That Food Library record is no longer available.",
            chat_id=chat_id,
        )
        return False
    known_data = {"food_finder_food_id": int(food_id)}
    known_data.update(return_data or {})
    update_conversation(
        chat_id=chat_id,
        current_step="food_library_manage",
        known_data=known_data,
        missing_fields=[],
    )
    send_telegram_msg(
        format_food_library_manage(food),
        chat_id=chat_id,
    )
    return True


def format_food_library_pantry_item(item: dict) -> str:
    """Show one linked Pantry placement from Food Library management."""
    storage = PANTRY_STORAGE_AREAS.get(
        str(item.get("storage_area") or "unsorted"),
        "Unsorted",
    )
    category = PANTRY_FOOD_CATEGORIES.get(
        str(item.get("food_category") or "unsorted"),
        "Unsorted",
    )
    return (
        "Food Library — Pantry Item\n\n"
        f"Item: {item.get('display_name') or 'Pantry item'}\n"
        f"Storage: {storage}\n"
        f"Food type: {category}\n\n"
        "1. Change location and type\n"
        "2. Remove from Pantry\n"
        "3. Back\n\n"
        "The Food Library nutrition record and past history are preserved."
    )


def show_food_library_pantry_item(
    *,
    chat_id: int | str,
    pantry_item_id: int,
) -> bool:
    """Refresh and show one Food Library Pantry placement."""
    item = get_pantry_item_by_id(pantry_item_id)
    if item is None:
        send_telegram_msg(
            "That Pantry item is no longer available.",
            chat_id=chat_id,
        )
        return False
    update_conversation(
        chat_id=chat_id,
        current_step="food_library_pantry_item",
        known_data={
            "food_library_pantry_item_id": int(pantry_item_id),
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_food_library_pantry_item(item),
        chat_id=chat_id,
    )
    return True


def format_food_library_pantry_choices(items: list[dict]) -> str:
    """Choose one of multiple Pantry placements linked to a Food."""
    lines = ["Food Library — Choose Pantry Item", ""]
    for index, item in enumerate(items, start=1):
        storage = PANTRY_STORAGE_AREAS.get(
            str(item.get("storage_area") or "unsorted"),
            "Unsorted",
        )
        lines.append(
            f"{index}. {item.get('display_name') or 'Pantry item'} "
            f"— {storage}"
        )
    lines.extend(["", "Choose a number or reply Back."])
    return "\n".join(lines)


def show_food_finder_results(
    *,
    chat_id: int | str,
    query: str,
) -> bool:
    """Search the Food Library and preserve a reviewable result list."""
    try:
        foods = search_food_locations(query)
    except ValueError as exc:
        send_telegram_msg(
            f"{exc}\n\nSend a more specific food name or reply Back.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return False

    if not foods:
        send_telegram_msg(
            "I couldn't find that name in the Food Library, My Pantry, "
            "Saved Recipes, Favorites, or previous food logs.\n\n"
            "Try another spelling, a shorter product name, or a brand. "
            "Nothing was changed.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return False

    update_conversation(
        chat_id=chat_id,
        current_step="food_finder_results",
        known_data={
            "food_finder_query": str(query).strip(),
            "food_finder_ids": [
                int(food["food_id"]) for food in foods
            ],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_food_finder_results(
            foods,
            query=str(query).strip(),
        ),
        chat_id=chat_id,
    )
    return True


def _food_duplicate_identity(food: dict) -> str:
    """Return one concise identity for duplicate-review messages."""
    name = str(food.get("canonical_name") or "Food")
    brand = str(food.get("brand") or "").strip()
    return f"{name} — {brand}" if brand else name


def _food_duplicate_record_lines(
    food: dict,
    *,
    label: str,
) -> list[str]:
    """Describe the future nutrition and preserved uses of one record."""
    def nutrient(field: str, suffix: str, *, decimals: int = 1) -> str:
        value = food.get(field)
        if value is None:
            return "not available"
        return (
            f"{format_display_number(float(value), decimals=decimals)}"
            f" {suffix}"
        ).strip()

    return [
        f"{label}: {_food_duplicate_identity(food)}",
        f"- Serving: {food.get('serving_description') or 'not available'}",
        "- Nutrition: "
        f"{nutrient('calories', 'cal', decimals=0)}; "
        f"protein {nutrient('protein_g', 'g')}; "
        f"carbs {nutrient('carbohydrates_g', 'g')}; "
        f"fat {nutrient('fat_g', 'g')}",
        "- Fiber/sugar/sodium: "
        f"{nutrient('fiber_g', 'g')} / "
        f"{nutrient('sugar_g', 'g')} / "
        f"{nutrient('sodium_mg', 'mg', decimals=0)}",
        f"- Source: {food.get('verification_source') or 'unknown'}",
        "- Current uses: "
        f"{int(food.get('pantry_count') or 0)} Pantry, "
        f"{int(food.get('favorite_count') or 0)} Favorites, "
        f"{int(food.get('barcode_count') or 0)} barcodes, "
        f"{int(food.get('log_count') or 0)} past logs, "
        f"{int(food.get('recipe_ingredient_count') or 0)} recipe ingredients",
    ]


def format_food_duplicate_choices(pairs: list[dict]) -> str:
    """Show possible duplicates without declaring that they are identical."""
    lines = [
        "Food Library Cleanup — Possible Duplicates",
        "",
        "These are name-based possibilities, not automatic matches.",
        "",
    ]
    for index, pair in enumerate(pairs, start=1):
        lines.append(
            f"{index}. {_food_duplicate_identity(pair['first'])} ↔ "
            f"{_food_duplicate_identity(pair['second'])}"
        )
    lines.extend([
        "",
        "Choose a number to compare both records. Nothing changes until "
        "you confirm.",
        "Reply Back or Cancel.",
    ])
    return "\n".join(lines)


def format_food_duplicate_comparison(
    first: dict,
    second: dict,
) -> str:
    """Show both records and safe review options."""
    lines = ["Food Library Cleanup — Compare", ""]
    lines.extend(_food_duplicate_record_lines(first, label="First"))
    lines.append("")
    lines.extend(_food_duplicate_record_lines(second, label="Second"))
    lines.extend([
        "",
        "If these are the same reusable food, choose which record should "
        "supply the future name, serving, and nutrition.",
        "",
        "1. Use first",
        "2. Use second",
        "3. Keep both — they are different foods",
        "4. Back",
        "",
        "Past food logs, Nutrition Versions, and saved recipe calculations "
        "will always remain unchanged.",
    ])
    return "\n".join(lines)


def show_food_duplicate_list(*, chat_id: int | str) -> bool:
    """Refresh the possible duplicate queue after each decision."""
    pairs = list_possible_food_duplicates()
    if not pairs:
        update_conversation(
            chat_id=chat_id,
            current_step="saved_foods",
            known_data={},
            missing_fields=[],
        )
        send_telegram_msg(
            "Food Library Cleanup\n\n"
            "No unreviewed possible duplicates were found. Nothing was "
            "changed.\n\n"
            + healthcoach_saved_foods_menu_text(),
            chat_id=chat_id,
        )
        return False
    pair_ids = [
        [int(pair["first"]["food_id"]), int(pair["second"]["food_id"])]
        for pair in pairs
    ]
    update_conversation(
        chat_id=chat_id,
        current_step="food_duplicate_list",
        known_data={"food_duplicate_pair_ids": pair_ids},
        missing_fields=[],
    )
    send_telegram_msg(
        format_food_duplicate_choices(pairs),
        chat_id=chat_id,
    )
    return True


FOOD_HEALTH_CHECK_PAGE_SIZE = 10
FOOD_HEALTH_CHECK_LABELS = {
    "nutrition_gaps": "Nutrition Details",
    "source_review": "Source Review",
    "source_rechecks": "Source Rechecks",
}


def format_food_library_health_check(report: dict) -> str:
    """Summarize actionable Food Library and Pantry maintenance."""
    preserved = int(report.get("preserved_versions") or 0)
    return (
        "Food Library Health Check\n\n"
        f"Reusable foods reviewed: {int(report.get('food_count') or 0)}\n"
        f"Pantry items reviewed: {int(report.get('pantry_count') or 0)}\n\n"
        "1. Complete Pantry nutrition — "
        f"{len(report.get('pantry_nutrition') or [])} need attention\n"
        "2. Organize Pantry — "
        f"{len(report.get('pantry_organization') or [])} unsorted\n"
        "3. Review nutrition gaps — "
        f"{len(report.get('nutrition_gaps') or [])} foods\n"
        "4. Review sources — "
        f"{len(report.get('source_review') or [])} foods\n"
        "5. Review source rechecks — "
        f"{len(report.get('source_rechecks') or [])} due\n"
        "6. Back\n\n"
        f"Previous Nutrition Versions on current foods: {preserved}. These "
        "are preserved on purpose so previous logs and saved recipe "
        "calculations never change.\n\n"
        "This is a review report. It never changes or deletes anything "
        "automatically."
    )


def show_food_library_health_check(*, chat_id: int | str) -> dict:
    """Refresh and display the read-only Food Library health check."""
    report = build_food_library_health_check()
    update_conversation(
        chat_id=chat_id,
        current_step="food_health_check",
        known_data={},
        missing_fields=[],
    )
    send_telegram_msg(
        format_food_library_health_check(report),
        chat_id=chat_id,
    )
    return report


def format_food_health_check_list(
    foods: list[dict],
    *,
    category: str,
    page: int,
) -> str:
    """Format one stable page of Food Library health-check findings."""
    page_foods, safe_page, total_pages = simple_page(
        foods,
        page=page,
        page_size=FOOD_HEALTH_CHECK_PAGE_SIZE,
    )
    label = FOOD_HEALTH_CHECK_LABELS[category]
    lines = [
        f"Food Library Health Check — {label}",
        f"Page {safe_page + 1} of {total_pages}",
        "",
    ]
    for index, food in enumerate(page_foods, start=1):
        brand = str(food.get("brand") or "").strip()
        identity = str(food.get("canonical_name") or "Food")
        if brand:
            identity += f" — {brand}"
        lines.append(f"{index}. {identity}")
        lines.append(f"   {health_check_food_reason(food, category)}")
    lines.extend([
        "",
        f"Items needing review: {len(foods)}",
        "Choose a number for details, Next, Previous, Back, or Cancel.",
        "Nothing changes unless you choose and confirm a management action.",
    ])
    return "\n".join(lines)


def show_food_health_check_list(
    *,
    chat_id: int | str,
    category: str,
    page: int = 0,
) -> bool:
    """Refresh one Food Library health-check category."""
    report = build_food_library_health_check()
    foods = health_check_food_items(report, category)
    if not foods:
        send_telegram_msg(
            f"{FOOD_HEALTH_CHECK_LABELS[category]} is clear. No foods need "
            "attention in this section.\n\n"
            + format_food_library_health_check(report),
            chat_id=chat_id,
        )
        update_conversation(
            chat_id=chat_id,
            current_step="food_health_check",
            known_data={},
            missing_fields=[],
        )
        return False
    page_foods, safe_page, _ = simple_page(
        foods,
        page=page,
        page_size=FOOD_HEALTH_CHECK_PAGE_SIZE,
    )
    update_conversation(
        chat_id=chat_id,
        current_step="food_health_check_list",
        known_data={
            "food_health_check_category": category,
            "food_health_check_page": safe_page,
            "food_health_check_ids": [
                int(food["food_id"]) for food in page_foods
            ],
        },
        missing_fields=[],
    )
    send_telegram_msg(
        format_food_health_check_list(
            foods,
            category=category,
            page=safe_page,
        ),
        chat_id=chat_id,
    )
    return True


def format_food_health_check_item(food: dict, *, category: str) -> str:
    """Show a health-check reason with the existing Food Finder details."""
    details = format_food_finder_details(food)
    details = details.split("\n\n", 1)[-1]
    details = details.replace(
        "Reply Manage Food, Search again, Back, or Cancel.",
        "Reply Manage Food, Back, or Cancel.",
    )
    return (
        "Food Library Health Check — Item\n\n"
        f"Why it is listed: {health_check_food_reason(food, category)}\n\n"
        + details
    )


def format_saved_food_choices(foods: list[dict]) -> str:
    lines = ["Choose an entered food to view:", ""]

    for index, food in enumerate(foods, start=1):
        lines.append(
            f"{index}. {food['canonical_name']} — "
            f"{food.get('serving_description') or '1 serving'}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_saved_food_edit_choices(
    foods: list[dict],
) -> str:
    lines = ["Choose an entered food to edit:", ""]

    for index, food in enumerate(foods, start=1):
        lines.append(
            f"{index}. {food['canonical_name']} — "
            f"{food.get('serving_description') or '1 serving'}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_saved_food_delete_choices(
    foods: list[dict],
) -> str:
    lines = ["Choose an entered food to remove:", ""]

    for index, food in enumerate(foods, start=1):
        lines.append(
            f"{index}. {food['canonical_name']} — "
            f"{food.get('serving_description') or '1 serving'}"
        )

    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


def format_saved_food_edit_menu(food: dict) -> str:
    return (
        "Entered Food Edit Menu\n\n"
        f"Food: {food.get('canonical_name') or 'Entered food'}\n"
        "Serving: "
        f"{food.get('serving_description') or '1 serving'}\n"
        "Current nutrition version: "
        f"{int(food.get('version_number') or 1)}\n\n"
        "1. Name\n"
        "2. Serving description\n"
        "3. Nutrition\n"
        "4. Back\n\n"
        "Nutrition changes apply only to future logs."
    )


def format_saved_food_details(food: dict) -> str:
    def nutrient(field: str, suffix: str) -> str:
        value = float(food.get(field) or 0)
        return (
            f"{format_display_number(value)} {suffix}"
        ).strip()

    source = str(
        food.get("verification_source") or "user entered"
    ).replace("_", " ").title()

    return (
        "Entered Food Details\n\n"
        f"Food: {food['canonical_name']}\n"
        f"Serving: "
        f"{food.get('serving_description') or '1 serving'}\n"
        f"Nutrition version: {food.get('version_number') or 1}\n"
        f"Source: {source}\n\n"
        f"Calories: {nutrient('calories', 'cal')}\n"
        f"Protein: {nutrient('protein_g', 'g')}\n"
        f"Carbohydrates: "
        f"{nutrient('carbohydrates_g', 'g')}\n"
        f"Fat: {nutrient('fat_g', 'g')}\n"
        f"Fiber: {nutrient('fiber_g', 'g')}\n"
        f"Sugar: {nutrient('sugar_g', 'g')}\n"
        f"Sodium: {nutrient('sodium_mg', 'mg')}\n\n"
        "Reply Edit Entered Food, Remove Entered Food, Back, or Cancel."
    )


def healthcoach_restaurant_menu_text() -> str:
    return (
        "Restaurant Menu\n\n"
        "1. Find best choices online\n"
        "2. Back\n\n"
        "Recommendations use current cited menu information. "
        "Nothing is logged automatically."
    )


def clean_restaurant_display_text(value) -> str:
    text = str(value or "")
    replacements = {
        "Â®": "",
        "â„¢": "",
        "â€™": "'",
        "â€“": "-",
        "â€”": "-",
        "®": "",
        "™": "",
        "’": "'",
        "–": "-",
        "—": "-",
    }
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)
    return text.strip()


def format_restaurant_advice(advice: dict) -> str:
    name = clean_restaurant_display_text(
        advice.get("restaurant_display_name")
        or "Restaurant"
    )
    candidates = list(advice.get("candidates") or [])

    if not advice.get("found") or not candidates:
        notes = list(advice.get("notes") or [])
        reason = notes[0] if notes else (
            "No supported current menu recommendations were found."
        )
        return (
            "Restaurant Recommendations\n\n"
            f"{name}\n\n"
            f"I could not find three supported choices. {reason}\n\n"
            "Try including the city and state, or check the restaurant "
            "name.\n\n"
            "Reply Search again, Back, or Cancel."
        )

    lines = ["Restaurant Recommendations", "", name]

    for index, candidate in enumerate(candidates, start=1):
        pick_label = (
            " — Heart-Healthy Pick"
            if candidate.get("heart_healthy_pick")
            else ""
        )
        lines.extend(
            [
                "",
                f"{index}. "
                f"{clean_restaurant_display_text(candidate['item_name'])}"
                f"{pick_label}",
            ]
        )
        if candidate.get("nutrition_status") == "official":
            nutrition_parts = []
            if candidate.get("calories") is not None:
                nutrition_parts.append(
                    f"{format_display_number(float(candidate['calories']), decimals=0)} cal"
                )
            if candidate.get("protein_g") is not None:
                nutrition_parts.append(
                    f"{format_display_number(float(candidate['protein_g']))} g protein"
                )
            lines.append(
                "Official nutrition: "
                + (
                    ", ".join(nutrition_parts)
                    if nutrition_parts
                    else "values not listed"
                )
            )
        else:
            lines.append(
                "Nutrition: not published; menu-based recommendation"
            )
        lines.append(
            f"Why: {clean_restaurant_display_text(candidate['recommendation_reason'])}"
        )
        if candidate.get("heart_healthy_pick"):
            lines.append(
                "Heart-healthy note: "
                + clean_restaurant_display_text(
                    candidate.get("heart_healthy_reason")
                )
            )
        lines.append(
            f"Source: {clean_restaurant_display_text(candidate['source_title'])}"
        )
        lines.append(str(candidate["source_url"]))

    if any(
        candidate.get("heart_healthy_pick")
        for candidate in candidates
    ):
        heart_label_note = (
            "Heart-Healthy Pick is based only on cited menu details "
            "and official nutrition when available. It is general "
            "food guidance, not a medical rating."
        )
    else:
        heart_label_note = (
            "No Heart-Healthy Pick was assigned because the cited "
            "menu details did not support one clearly."
        )

    lines.extend(
        [
            "",
            heart_label_note,
            "",
            "Menu availability can change. Nothing has been logged.",
            "",
            "Reply Search again, Back, or Cancel.",
        ]
    )
    return "\n".join(lines)


def healthcoach_favorites_menu_text() -> str:
    return (
        "Favorites Menu\n\n"
        "1. Quick log\n"
        "2. Save today's food\n"
        "3. Remove favorite\n"
        "4. Back"
    )


def healthcoach_health_menu_text() -> str:
    return (
        "Health Menu\n\n"
        "1. Current status\n"
        "2. Record sleep\n"
        "3. Record weight\n"
        "4. Health history\n"
        "5. Daily health insight\n"
        "6. Weekly health insight\n"
        "7. Back"
    )


def healthcoach_health_history_menu_text() -> str:
    return (
        "Health History\n\n"
        "Choose how much weight, sleep, exercise, resting "
        "heart-rate, HRV, Cardio Fitness, walking heart-rate, and "
        "blood-pressure history "
        "to view.\n\n"
        "1. Last 7 days\n"
        "2. Last 14 days\n"
        "3. Last 30 days\n"
        "4. Back"
    )


def build_health_history_data(
    *,
    reference_date,
    days: int,
    rows: list,
) -> dict:
    period_days = int(days)
    if period_days not in {7, 14, 30}:
        raise ValueError("days must be 7, 14, or 30.")

    start_date = reference_date - timedelta(
        days=period_days - 1
    )
    metrics_by_date = {}

    for row in rows:
        row_date = parse_row_date(row)
        if row_date is None:
            continue
        if not start_date <= row_date <= reference_date:
            continue
        metrics = row_to_metrics(row)
        if metrics is not None:
            metrics_by_date[row_date] = metrics

    history_days = []
    weights = []
    sleep_values = []
    exercise_values = []
    resting_heart_rate_values = []
    hrv_values = []
    cardio_fitness_values = []
    walking_heart_rate_values = []
    blood_pressure_systolic_values = []
    blood_pressure_diastolic_values = []

    for offset in range(period_days):
        current_date = start_date + timedelta(days=offset)
        metrics = metrics_by_date.get(current_date) or {}
        weight = metrics.get("weight")
        sleep_hours = metrics.get("sleep_hours")
        exercise_minutes = metrics.get("exercise_minutes")
        resting_heart_rate = metrics.get("rhr")
        hrv = metrics.get("hrv")
        cardio_fitness = metrics.get("cardio_fitness")
        walking_heart_rate = metrics.get(
            "walking_heart_rate_average"
        )
        blood_pressure_systolic = metrics.get(
            "blood_pressure_systolic"
        )
        blood_pressure_diastolic = metrics.get(
            "blood_pressure_diastolic"
        )
        blood_pressure_measured_at = metrics.get(
            "blood_pressure_measured_at"
        )

        if weight is not None:
            weights.append(float(weight))
        if sleep_hours is not None:
            sleep_values.append(float(sleep_hours))
        if exercise_minutes is not None:
            exercise_values.append(float(exercise_minutes))
        if resting_heart_rate is not None:
            resting_heart_rate_values.append(
                float(resting_heart_rate)
            )
        if hrv is not None:
            hrv_values.append(float(hrv))
        if cardio_fitness is not None:
            cardio_fitness_values.append(float(cardio_fitness))
        if walking_heart_rate is not None:
            walking_heart_rate_values.append(
                float(walking_heart_rate)
            )
        if (
            blood_pressure_systolic is not None
            and blood_pressure_diastolic is not None
            and blood_pressure_measured_at is not None
        ):
            blood_pressure_systolic_values.append(
                float(blood_pressure_systolic)
            )
            blood_pressure_diastolic_values.append(
                float(blood_pressure_diastolic)
            )

        history_days.append(
            {
                "date": current_date,
                "weight": (
                    float(weight)
                    if weight is not None
                    else None
                ),
                "sleep_hours": (
                    float(sleep_hours)
                    if sleep_hours is not None
                    else None
                ),
                "exercise_minutes": (
                    float(exercise_minutes)
                    if exercise_minutes is not None
                    else None
                ),
                "resting_heart_rate": (
                    float(resting_heart_rate)
                    if resting_heart_rate is not None
                    else None
                ),
                "hrv": (
                    float(hrv)
                    if hrv is not None
                    else None
                ),
                "cardio_fitness": (
                    float(cardio_fitness)
                    if cardio_fitness is not None
                    else None
                ),
                "walking_heart_rate_average": (
                    float(walking_heart_rate)
                    if walking_heart_rate is not None
                    else None
                ),
                "blood_pressure_systolic": (
                    float(blood_pressure_systolic)
                    if blood_pressure_systolic is not None
                    else None
                ),
                "blood_pressure_diastolic": (
                    float(blood_pressure_diastolic)
                    if blood_pressure_diastolic is not None
                    else None
                ),
                "blood_pressure_measured_at": (
                    blood_pressure_measured_at
                    if blood_pressure_measured_at is not None
                    else None
                ),
            }
        )

    return {
        "period_days": period_days,
        "start_date": start_date,
        "end_date": reference_date,
        "days": history_days,
        "average_weight": (
            sum(weights) / len(weights)
            if weights
            else None
        ),
        "weight_change": (
            weights[-1] - weights[0]
            if len(weights) >= 2
            else None
        ),
        "weight_entries": len(weights),
        "average_sleep": (
            sum(sleep_values) / len(sleep_values)
            if sleep_values
            else None
        ),
        "sleep_entries": len(sleep_values),
        "average_exercise_minutes": (
            sum(exercise_values) / len(exercise_values)
            if exercise_values
            else None
        ),
        "exercise_entries": len(exercise_values),
        "average_resting_heart_rate": (
            sum(resting_heart_rate_values)
            / len(resting_heart_rate_values)
            if resting_heart_rate_values
            else None
        ),
        "resting_heart_rate_entries": len(
            resting_heart_rate_values
        ),
        "average_hrv": (
            sum(hrv_values) / len(hrv_values)
            if hrv_values
            else None
        ),
        "hrv_change": (
            hrv_values[-1] - hrv_values[0]
            if len(hrv_values) >= 2
            else None
        ),
        "hrv_entries": len(hrv_values),
        "average_cardio_fitness": (
            sum(cardio_fitness_values)
            / len(cardio_fitness_values)
            if cardio_fitness_values
            else None
        ),
        "cardio_fitness_change": (
            cardio_fitness_values[-1] - cardio_fitness_values[0]
            if len(cardio_fitness_values) >= 2
            else None
        ),
        "cardio_fitness_entries": len(cardio_fitness_values),
        "average_walking_heart_rate": (
            sum(walking_heart_rate_values)
            / len(walking_heart_rate_values)
            if walking_heart_rate_values
            else None
        ),
        "walking_heart_rate_change": (
            walking_heart_rate_values[-1]
            - walking_heart_rate_values[0]
            if len(walking_heart_rate_values) >= 2
            else None
        ),
        "walking_heart_rate_entries": len(
            walking_heart_rate_values
        ),
        "average_blood_pressure_systolic": (
            sum(blood_pressure_systolic_values)
            / len(blood_pressure_systolic_values)
            if blood_pressure_systolic_values
            else None
        ),
        "average_blood_pressure_diastolic": (
            sum(blood_pressure_diastolic_values)
            / len(blood_pressure_diastolic_values)
            if blood_pressure_diastolic_values
            else None
        ),
        "blood_pressure_entries": len(
            blood_pressure_systolic_values
        ),
    }


def format_health_history(history: dict) -> str:
    period_days = int(history.get("period_days") or 0)
    missing_text = "—" if period_days == 30 else "not recorded"
    lines = [
        f"Health History - Last {period_days} Days",
        "",
    ]

    for item in history.get("days") or []:
        day = item["date"]
        weight = item.get("weight")
        sleep_hours = item.get("sleep_hours")
        exercise_minutes = item.get("exercise_minutes")
        resting_heart_rate = item.get("resting_heart_rate")
        hrv = item.get("hrv")
        cardio_fitness = item.get("cardio_fitness")
        walking_heart_rate = item.get(
            "walking_heart_rate_average"
        )
        blood_pressure_systolic = item.get(
            "blood_pressure_systolic"
        )
        blood_pressure_diastolic = item.get(
            "blood_pressure_diastolic"
        )
        weight_text = (
            f"{format_display_number(weight)} lb"
            if weight is not None
            else missing_text
        )
        sleep_text = (
            f"{format_display_number(sleep_hours)} h"
            if sleep_hours is not None
            else missing_text
        )
        exercise_text = (
            f"{format_display_number(exercise_minutes)} min"
            if exercise_minutes is not None
            else missing_text
        )
        resting_heart_rate_text = (
            f"{format_display_number(resting_heart_rate)} bpm"
            if resting_heart_rate is not None
            else missing_text
        )
        hrv_text = (
            f"{format_display_number(hrv)} ms"
            if hrv is not None
            else missing_text
        )
        cardio_fitness_text = (
            f"{format_display_number(cardio_fitness)}"
            if cardio_fitness is not None
            else missing_text
        )
        walking_heart_rate_text = (
            f"{format_display_number(walking_heart_rate)} bpm"
            if walking_heart_rate is not None
            else missing_text
        )
        blood_pressure_text = (
            f"{format_display_number(blood_pressure_systolic)}/"
            f"{format_display_number(blood_pressure_diastolic)} mmHg"
            if (
                blood_pressure_systolic is not None
                and blood_pressure_diastolic is not None
            )
            else missing_text
        )
        if period_days == 30:
            lines.append(
                f"{day.strftime('%a %b ')}{day.day}: "
                f"wt {weight_text}; sl {sleep_text}; "
                f"ex {exercise_text}; RHR {resting_heart_rate_text}; "
                f"HRV {hrv_text}; "
                f"CF {cardio_fitness_text}; WHR "
                f"{walking_heart_rate_text}; BP {blood_pressure_text}"
            )
        else:
            lines.append(
                f"{day.strftime('%a %b ')}{day.day}: "
                f"weight {weight_text}; sleep {sleep_text}; "
                f"exercise {exercise_text}; resting HR "
                f"{resting_heart_rate_text}; HRV {hrv_text}; "
                f"cardio fitness "
                f"{cardio_fitness_text}; walking HR "
                f"{walking_heart_rate_text}; blood pressure "
                f"{blood_pressure_text}"
            )

    lines.extend(["", "Summary"])
    average_weight = history.get("average_weight")
    weight_change = history.get("weight_change")
    average_sleep = history.get("average_sleep")
    average_exercise = history.get(
        "average_exercise_minutes"
    )
    average_resting_heart_rate = history.get(
        "average_resting_heart_rate"
    )
    average_hrv = history.get("average_hrv")
    hrv_change = history.get("hrv_change")
    average_cardio_fitness = history.get(
        "average_cardio_fitness"
    )
    cardio_fitness_change = history.get(
        "cardio_fitness_change"
    )
    average_walking_heart_rate = history.get(
        "average_walking_heart_rate"
    )
    walking_heart_rate_change = history.get(
        "walking_heart_rate_change"
    )
    average_blood_pressure_systolic = history.get(
        "average_blood_pressure_systolic"
    )
    average_blood_pressure_diastolic = history.get(
        "average_blood_pressure_diastolic"
    )

    lines.append(
        "- Average weight: "
        + (
            f"{format_display_number(average_weight)} lb"
            if average_weight is not None
            else "not available"
        )
    )
    lines.append(
        "- Recorded weight change: "
        + (
            f"{float(weight_change):+.1f} lb"
            if weight_change is not None
            else "not available"
        )
    )
    lines.append(
        "- Average sleep: "
        + (
            f"{format_display_number(average_sleep)} h"
            if average_sleep is not None
            else "not available"
        )
    )
    lines.append(
        "- Weight recorded: "
        f"{int(history.get('weight_entries') or 0)}/{period_days} days"
    )
    lines.append(
        "- Sleep recorded: "
        f"{int(history.get('sleep_entries') or 0)}/{period_days} days"
    )
    lines.append(
        "- Average Exercise Minutes: "
        + (
            f"{format_display_number(average_exercise)} min"
            if average_exercise is not None
            else "not available"
        )
    )
    lines.append(
        "- Exercise recorded: "
        f"{int(history.get('exercise_entries') or 0)}/{period_days} days"
    )
    lines.append(
        "- Average resting heart rate: "
        + (
            f"{format_display_number(average_resting_heart_rate)} bpm"
            if average_resting_heart_rate is not None
            else "not available"
        )
    )
    lines.append(
        "- Resting heart rate recorded: "
        f"{int(history.get('resting_heart_rate_entries') or 0)}"
        f"/{period_days} days"
    )
    lines.append(
        "- Average HRV: "
        + (
            f"{format_display_number(average_hrv)} ms"
            if average_hrv is not None
            else "not available"
        )
    )
    lines.append(
        "- Recorded HRV change: "
        + (
            f"{float(hrv_change):+.1f} ms"
            if hrv_change is not None
            else "not available"
        )
    )
    lines.append(
        "- HRV recorded: "
        f"{int(history.get('hrv_entries') or 0)}"
        f"/{period_days} days"
    )
    lines.append(
        "- Average Cardio Fitness: "
        + (
            f"{format_display_number(average_cardio_fitness)} "
            "mL/kg/min"
            if average_cardio_fitness is not None
            else "not available"
        )
    )
    lines.append(
        "- Recorded Cardio Fitness change: "
        + (
            f"{float(cardio_fitness_change):+.1f} mL/kg/min"
            if cardio_fitness_change is not None
            else "not available"
        )
    )
    lines.append(
        "- Cardio Fitness recorded: "
        f"{int(history.get('cardio_fitness_entries') or 0)}"
        f"/{period_days} days"
    )
    lines.append(
        "- Average walking heart rate: "
        + (
            f"{format_display_number(average_walking_heart_rate)} bpm"
            if average_walking_heart_rate is not None
            else "not available"
        )
    )
    lines.append(
        "- Recorded walking heart-rate change: "
        + (
            f"{float(walking_heart_rate_change):+.1f} bpm"
            if walking_heart_rate_change is not None
            else "not available"
        )
    )
    lines.append(
        "- Walking heart rate recorded: "
        f"{int(history.get('walking_heart_rate_entries') or 0)}"
        f"/{period_days} days"
    )
    lines.append(
        "- Average blood pressure: "
        + (
            f"{format_display_number(average_blood_pressure_systolic)}/"
            f"{format_display_number(average_blood_pressure_diastolic)} "
            "mmHg"
            if (
                average_blood_pressure_systolic is not None
                and average_blood_pressure_diastolic is not None
            )
            else "not available"
        )
    )
    lines.append(
        "- Blood pressure recorded: "
        f"{int(history.get('blood_pressure_entries') or 0)}"
        f"/{period_days} days"
    )
    lines.extend(
        [
            "",
            "Weight change compares the first and last recorded "
            "weights in this period.",
            *(
                ["— means not recorded."]
                if period_days == 30
                else []
            ),
            "Reply 7 days, 14 days, 30 days, Back, or Cancel.",
        ]
    )
    return "\n".join(lines)


def get_formatted_health_history(
    *,
    reference_date,
    days: int,
) -> str:
    rows = get_recent_rows(
        reference_date,
        days_back=int(days) - 1,
    )
    history = build_health_history_data(
        reference_date=reference_date,
        days=days,
        rows=rows,
    )
    return format_health_history(history)


def build_daily_health_records(rows: list) -> list[dict]:
    """Convert Tracker rows without changing blank values into zeros."""
    records = []
    for row in rows:
        row_date = parse_row_date(row)
        metrics = row_to_metrics(row)
        if row_date is None or metrics is None:
            continue
        padded = list(row) + [""] * (len(HEADERS) - len(row))

        def recorded(index: int, value):
            return value if padded[index] not in ("", None) else None

        records.append({
            "date": row_date.isoformat(),
            "steps": recorded(1, metrics.get("steps")),
            "total_burn": recorded(2, metrics.get("total_cals")),
            "active_calories": recorded(
                3,
                metrics.get("active_cals"),
            ),
            "sleep_hours": metrics.get("sleep_hours"),
            "resting_heart_rate": metrics.get("rhr"),
            "weight": metrics.get("weight"),
            "hrv": metrics.get("hrv"),
            "exercise_minutes": metrics.get("exercise_minutes"),
            "cardio_fitness": metrics.get("cardio_fitness"),
            "walking_heart_rate": metrics.get(
                "walking_heart_rate_average"
            ),
            "blood_pressure_systolic": metrics.get(
                "blood_pressure_systolic"
            ),
            "blood_pressure_diastolic": metrics.get(
                "blood_pressure_diastolic"
            ),
        })
    return records


def get_daily_health_insight_message(
    *,
    reference_date: date,
    now: datetime | None = None,
) -> str:
    """Read personal history and return one grounded daily insight."""
    current_time = now or datetime.now(PACIFIC_TZ)
    rows = get_recent_rows(
        reference_date,
        days_back=29,
    )
    evidence = build_daily_health_evidence(
        records=build_daily_health_records(rows),
        reference_date=reference_date,
        now_hour=current_time.hour,
    )
    if not evidence.get("facts"):
        raise ValueError(
            "There is not enough recorded health data for an insight yet."
        )

    personalized = True
    try:
        narrative = generate_daily_health_narrative(evidence)
    except Exception:
        personalized = False
        logging.exception(
            "Personalized Daily Health Insight generation failed"
        )
        narrative = fallback_daily_health_narrative(evidence)

    return format_daily_health_insight(
        evidence,
        narrative,
        personalized=personalized,
    )


def get_weekly_health_insight_message(
    *,
    reference_date: date,
) -> str:
    """Return one grounded comparison of completed health weeks."""
    rows = get_recent_rows(
        reference_date,
        days_back=34,
    )
    evidence = build_weekly_health_evidence(
        records=build_daily_health_records(rows),
        reference_date=reference_date,
    )
    if not evidence.get("facts"):
        raise ValueError(
            "There is not enough completed health data for a weekly "
            "insight yet."
        )
    personalized = True
    try:
        narrative = generate_weekly_health_narrative(evidence)
    except Exception:
        personalized = False
        logging.exception(
            "Personalized Weekly Health Insight generation failed"
        )
        narrative = fallback_weekly_health_narrative(evidence)
    return format_weekly_health_insight(
        evidence,
        narrative,
        personalized=personalized,
    )


def healthcoach_heart_health_menu_text() -> str:
    return (
        "Heart Health Report\n\n"
        "Choose how much recorded heart-health history to "
        "summarize.\n\n"
        "1. Last 7 days\n"
        "2. Last 14 days\n"
        "3. Last 30 days\n"
        "4. Back\n\n"
        "This report shows recorded trends only. It does not "
        "diagnose or assign a medical risk rating."
    )


def recorded_metric_change(
    history: dict,
    field: str,
) -> float | None:
    values = [
        float(item[field])
        for item in history.get("days") or []
        if item.get(field) is not None
    ]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def format_recorded_change(
    value: float | None,
    unit: str,
) -> str:
    if value is None:
        return "not available"
    return f"{float(value):+.1f} {unit}"


def format_heart_health_report(history: dict) -> str:
    period_days = int(history.get("period_days") or 0)
    start_date = history.get("start_date")
    end_date = history.get("end_date")

    resting_change = recorded_metric_change(
        history,
        "resting_heart_rate",
    )
    hrv_change = recorded_metric_change(
        history,
        "hrv",
    )
    cardio_change = recorded_metric_change(
        history,
        "cardio_fitness",
    )
    walking_change = recorded_metric_change(
        history,
        "walking_heart_rate_average",
    )

    blood_pressure_days = [
        item
        for item in history.get("days") or []
        if (
            item.get("blood_pressure_systolic") is not None
            and item.get("blood_pressure_diastolic") is not None
        )
    ]
    latest_blood_pressure = (
        blood_pressure_days[-1]
        if blood_pressure_days
        else None
    )

    def average_text(field: str, unit: str) -> str:
        value = history.get(field)
        if value is None:
            return "not available"
        return f"{format_display_number(value)} {unit}".strip()

    period_text = ""
    if start_date is not None and end_date is not None:
        period_text = (
            f"{start_date.strftime('%b ')}{start_date.day}–"
            f"{end_date.strftime('%b ')}{end_date.day}, "
            f"{end_date.year}"
        )

    lines = [
        f"Heart Health Report - Last {period_days} Days",
        period_text,
        "",
        "Heart measurements",
        "- Resting heart rate: average "
        + average_text("average_resting_heart_rate", "bpm")
        + "; recorded change "
        + format_recorded_change(resting_change, "bpm")
        + "; recorded "
        + f"{int(history.get('resting_heart_rate_entries') or 0)}"
        + f"/{period_days} days",
        "- HRV: average "
        + average_text("average_hrv", "ms")
        + "; recorded change "
        + format_recorded_change(hrv_change, "ms")
        + "; recorded "
        + f"{int(history.get('hrv_entries') or 0)}"
        + f"/{period_days} days",
        "- Cardio Fitness: average "
        + average_text("average_cardio_fitness", "mL/kg/min")
        + "; recorded change "
        + format_recorded_change(cardio_change, "mL/kg/min")
        + "; recorded "
        + f"{int(history.get('cardio_fitness_entries') or 0)}"
        + f"/{period_days} days",
        "- Walking heart rate: average "
        + average_text("average_walking_heart_rate", "bpm")
        + "; recorded change "
        + format_recorded_change(walking_change, "bpm")
        + "; recorded "
        + f"{int(history.get('walking_heart_rate_entries') or 0)}"
        + f"/{period_days} days",
    ]

    average_systolic = history.get(
        "average_blood_pressure_systolic"
    )
    average_diastolic = history.get(
        "average_blood_pressure_diastolic"
    )
    if (
        average_systolic is not None
        and average_diastolic is not None
    ):
        average_blood_pressure_text = (
            f"{format_display_number(average_systolic)}/"
            f"{format_display_number(average_diastolic)} mmHg"
        )
    else:
        average_blood_pressure_text = "not available"

    latest_blood_pressure_text = "not available"
    if latest_blood_pressure is not None:
        latest_blood_pressure_text = (
            f"{format_display_number(latest_blood_pressure['blood_pressure_systolic'])}/"
            f"{format_display_number(latest_blood_pressure['blood_pressure_diastolic'])} "
            "mmHg on "
            f"{latest_blood_pressure['date'].strftime('%a %b ')}"
            f"{latest_blood_pressure['date'].day}"
        )

    lines.extend(
        [
            "- Blood pressure: average "
            + average_blood_pressure_text
            + "; latest "
            + latest_blood_pressure_text
            + "; recorded "
            + f"{int(history.get('blood_pressure_entries') or 0)}"
            + f"/{period_days} days",
            "",
            "Activity and recovery context",
            "- Exercise Minutes: average "
            + average_text("average_exercise_minutes", "min")
            + "; recorded "
            + f"{int(history.get('exercise_entries') or 0)}"
            + f"/{period_days} days",
            "- Sleep: average "
            + average_text("average_sleep", "h")
            + "; recorded "
            + f"{int(history.get('sleep_entries') or 0)}"
            + f"/{period_days} days",
            "- Weight: average "
            + average_text("average_weight", "lb")
            + "; recorded change "
            + format_recorded_change(
                history.get("weight_change"),
                "lb",
            )
            + "; recorded "
            + f"{int(history.get('weight_entries') or 0)}"
            + f"/{period_days} days",
            "",
            "How to read this",
            "- Averages use recorded days only.",
            "- Changes compare the first and last recorded values.",
            "- Missing readings stay missing and are never treated as zero.",
            "",
            "Food support",
            "- Pantry Meal Ideas includes a Heart-Healthy Pick.",
            "- Smart Pantry swaps can suggest practical replacements.",
            "",
            "This report organizes recorded trends only. It does not "
            "diagnose, score cardiovascular risk, or classify a single "
            "reading. Discuss medical interpretation with a clinician.",
            "",
            "Reply 7 days, 14 days, 30 days, Back, or Cancel.",
        ]
    )
    return "\n".join(lines)


def get_formatted_heart_health_report(
    *,
    reference_date,
    days: int,
) -> str:
    rows = get_recent_rows(
        reference_date,
        days_back=int(days) - 1,
    )
    history = build_health_history_data(
        reference_date=reference_date,
        days=days,
        rows=rows,
    )
    return format_heart_health_report(history)


def healthcoach_reports_menu_text() -> str:
    return (
        "Reports Menu\n\n"
        "1. Today's summary\n"
        "2. Weekly report\n"
        "3. Goals\n"
        "4. Heart health\n"
        "5. Back"
    )


def healthcoach_goals_menu_text() -> str:
    return (
        "Goals Menu\n\n"
        "1. View active goal\n"
        "2. Add weight goal\n"
        "3. Update goal\n"
        "4. Edit goal\n"
        "5. Remove goal\n"
        "6. Goal history\n"
        "7. Back\n\n"
        "Weight goals update only when you choose Update goal."
    )


def parse_weight_goal_date(value: str, *, reference_date: date) -> date | None:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    if not cleaned:
        return None

    formats = [
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
    ]
    for date_format in formats:
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            pass

    for date_format in ("%B %d", "%b %d", "%m/%d"):
        try:
            parsed = datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
        candidate = parsed.replace(year=reference_date.year)
        if candidate <= reference_date:
            candidate = candidate.replace(year=reference_date.year + 1)
        return candidate

    return None


def parse_weight_goal_weight(value: str) -> float | None:
    match = re.search(r"(?<!\d)(\d{2,3}(?:\.\d+)?)", str(value or ""))
    if not match:
        return None
    weight = float(match.group(1))
    if not 75 <= weight <= 700:
        return None
    return weight


def get_weight_goal_health_inputs(*, reference_date) -> dict:
    """Read goal inputs without changing Health Tracker facts."""
    weight_rows = get_recent_rows(
        reference_date=reference_date,
        days_back=30,
    )
    latest_weight = None
    latest_weight_date = None
    for row in reversed(weight_rows):
        metrics = row_to_metrics(row) or {}
        if metrics.get("weight") is None:
            continue
        latest_weight = float(metrics["weight"])
        latest_weight_date = parse_row_date(row)
        break

    completed_reference = reference_date - timedelta(days=1)
    burn_rows = get_recent_rows(
        reference_date=completed_reference,
        days_back=30,
    )
    burn_values = collect_recent_numeric_values(
        burn_rows,
        2,
        limit=7,
        minimum_valid=0,
    )

    return {
        "current_weight": latest_weight,
        "weight_date": latest_weight_date,
        "average_daily_burn": (
            sum(burn_values) / len(burn_values)
            if burn_values
            else None
        ),
        "burn_days": len(burn_values),
    }


def format_weight_goal(goal: dict | None) -> str:
    if goal is None:
        return (
            "Active Weight Goal\n\n"
            "No active weight goal is saved.\n\n"
            "Choose Add weight goal to create one."
        )

    target_date = datetime.strptime(
        str(goal["target_date"]),
        "%Y-%m-%d",
    ).date()
    calculation = get_latest_weight_goal_calculation(
        int(goal["weight_goal_id"])
    )
    lines = [
        "Active Weight Goal",
        "",
        f"Starting weight: {format_display_number(goal['start_weight'])} lb",
        f"Goal weight: {format_display_number(goal['target_weight'])} lb",
        f"Goal date: {target_date.strftime('%b')} {target_date.day}, "
        f"{target_date.year}",
    ]

    if calculation is None:
        lines.extend(
            [
                "",
                "No calorie target has been calculated yet.",
                "Choose Update goal when you want a new calculation.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Saved calorie target: "
                f"{format_display_number(calculation['calorie_target_low'], decimals=0)}-"
                f"{format_display_number(calculation['calorie_target_high'], decimals=0)} "
                "calories/day",
                "Last updated: "
                f"{calculation['calculation_date']}",
                "7-day burn basis: "
                f"{format_display_number(calculation['average_daily_burn'], decimals=0)} "
                f"calories ({calculation['burn_days']} days)",
            ]
        )

    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


def update_and_format_weight_goal(*, reference_date) -> str:
    goal = get_active_weight_goal()
    if goal is None:
        raise ValueError(
            "No active weight goal exists. Add a weight goal first."
        )

    inputs = get_weight_goal_health_inputs(reference_date=reference_date)
    if inputs["current_weight"] is None:
        raise ValueError(
            "No recent official weight is available. Record your morning "
            "weight, then try Update goal again."
        )
    if int(inputs["burn_days"]) < 3:
        raise ValueError(
            "At least 3 completed days of total-burn data are needed. "
            "Nothing was recalculated."
        )

    target_date = datetime.strptime(
        str(goal["target_date"]),
        "%Y-%m-%d",
    ).date()
    result = calculate_weight_goal(
        current_date=reference_date,
        current_weight=float(inputs["current_weight"]),
        target_weight=float(goal["target_weight"]),
        target_date=target_date,
        average_daily_burn=float(inputs["average_daily_burn"]),
        burn_days=int(inputs["burn_days"]),
    )
    saved = save_weight_goal_calculation(
        int(goal["weight_goal_id"]),
        result,
    )

    lines = [
        "Weight Goal Updated",
        "",
        f"Current official weight: {format_display_number(saved['current_weight'])} lb",
        f"Goal: {format_display_number(goal['target_weight'])} lb by "
        f"{target_date.strftime('%b')} {target_date.day}, {target_date.year}",
        "7-day average burn: "
        f"{format_display_number(saved['average_daily_burn'], decimals=0)} "
        f"calories ({saved['burn_days']} completed days)",
        "Saved calorie target: "
        f"{format_display_number(saved['calorie_target_low'], decimals=0)}-"
        f"{format_display_number(saved['calorie_target_high'], decimals=0)} "
        "calories/day",
    ]

    if bool(saved["safely_reachable"]):
        required_weekly = float(saved["required_weekly_loss"])
        lines.extend(
            [
                "",
                "This goal is within the safety limits at about "
                f"{required_weekly:.2f} lb per week.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "The original goal is not safely reachable by that date "
                "under the current limits.",
                "Projected weight at the saved calorie target: "
                f"{float(saved['projected_weight']):.1f} lb",
                "Safety limit reached: "
                f"{saved.get('limiting_reason') or 'calorie safety limits'}.",
            ]
        )

    lines.extend(
        [
            "",
            "This is planning guidance, not a medical diagnosis. The target "
            "will not change again until you choose Update goal.",
        ]
    )
    return "\n".join(lines)


def format_weight_goal_history() -> str:
    goals = list_weight_goals()
    if not goals:
        return "Weight Goal History\n\nNo weight goals have been saved."

    lines = ["Weight Goal History", ""]
    for goal in goals:
        target_date = datetime.strptime(
            str(goal["target_date"]),
            "%Y-%m-%d",
        ).date()
        lines.append(
            f"- {format_display_number(goal['target_weight'])} lb by "
            f"{target_date.strftime('%b')} {target_date.day}, "
            f"{target_date.year} — {str(goal['status']).title()}"
        )
    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


def _format_goal_calorie_progress(entry_date) -> str:
    """Use the saved goal snapshot; never recalculate during logging."""
    today = datetime.now(PACIFIC_TZ).date()
    if entry_date != today:
        return ""

    calculation = get_latest_weight_goal_calculation()
    if calculation is None:
        return ""

    entries = list_food_entries(entry_date=entry_date)
    consumed = sum(
        float(entry["calories"])
        for entry in entries
        if entry.get("calories") is not None
    )
    missing_calories = sum(
        1 for entry in entries if entry.get("calories") is None
    )
    low = float(calculation["calorie_target_low"])
    high = float(calculation["calorie_target_high"])

    lines = [
        "Goal calories today",
        f"Eaten: {format_display_number(consumed, decimals=0)}",
        "Saved target: "
        f"{format_display_number(low, decimals=0)}-"
        f"{format_display_number(high, decimals=0)}",
    ]
    if consumed < low:
        lines.append(
            "Remaining: "
            f"{format_display_number(low - consumed, decimals=0)}-"
            f"{format_display_number(high - consumed, decimals=0)} calories"
        )
    elif consumed <= high:
        lines.append(
            "You are within the saved range; "
            f"{format_display_number(high - consumed, decimals=0)} calories "
            "remain to its upper end."
        )
    else:
        lines.append(
            f"Today is {format_display_number(consumed - high, decimals=0)} "
            "calories above the saved range. Tomorrow starts fresh."
        )

    if missing_calories:
        lines.append(
            f"Note: {missing_calories} logged item(s) have no calorie value, "
            "so this total may be incomplete."
        )
    return "\n".join(lines)


def format_goal_calorie_progress(entry_date) -> str:
    """Keep an optional goal message from ever breaking Food logging."""
    try:
        return _format_goal_calorie_progress(entry_date)
    except Exception:
        logging.exception("Saved goal calorie progress lookup failed")
        return ""


def append_goal_calorie_progress(message: str, entry_date) -> str:
    progress = format_goal_calorie_progress(entry_date)
    return message + ("\n\n" + progress if progress else "")


def format_daily_food_log(entry_date) -> str:
    entries = list_food_entries(entry_date=entry_date)

    if not entries:
        return "No foods are logged for today."

    lines = ["Today's Food Log"]
    current_meal = None

    for entry in entries:
        meal = str(entry.get("meal_category") or "Other").title()

        if meal != current_meal:
            lines.extend(["", meal])
            current_meal = meal

        name = format_food_display_name(
            canonical_name=str(
                entry.get("canonical_name") or "Food"
            ),
            restaurant=entry.get("restaurant"),
            size=None,
        )
        quantity = float(entry.get("quantity") or 1.0)
        calories = float(entry.get("calories") or 0.0)
        protein = float(entry.get("protein_g") or 0.0)

        lines.append(
            "- "
            f"{format_display_number(quantity)} × {name}: "
            f"{format_display_number(calories, decimals=0)} cal, "
            f"{format_display_number(protein)} g protein"
        )

    totals = get_daily_totals(entry_date)
    lines.extend(
        [
            "",
            (
                "Total: "
                f"{format_display_number(totals['calories'], decimals=0)} "
                "calories, "
                f"{format_display_number(totals['protein_g'])} g protein"
            ),
        ]
    )
    return "\n".join(lines)


MEAL_DISPLAY_ORDER = (
    "before breakfast",
    "breakfast",
    "school snack",
    "lunch",
    "afternoon snack",
    "dinner",
    "dessert",
)


def format_previous_food_entries(
    entries: list[dict],
) -> list[str]:
    lines: list[str] = []
    current_meal = None
    meal_positions = {
        meal: index
        for index, meal in enumerate(MEAL_DISPLAY_ORDER)
    }
    ordered_entries = sorted(
        entries,
        key=lambda entry: (
            meal_positions.get(
                str(entry.get("meal_category") or ""),
                len(meal_positions),
            ),
            int(entry.get("food_entry_id") or 0),
        ),
    )
    for entry in ordered_entries:
        meal = str(entry.get("meal_category") or "other")
        if meal != current_meal:
            lines.extend(["", meal.title()])
            current_meal = meal
        lines.append(
            "- "
            f"{format_display_number(float(entry.get('quantity') or 1))} × "
            f"{entry.get('canonical_name') or 'Food'}: "
            f"{format_display_number(float(entry.get('calories') or 0), decimals=0)} cal"
        )
    return lines


def format_yesterday_food_review(
    entries: list[dict],
    *,
    source_date,
) -> str:
    total_calories = sum(
        float(entry.get("calories") or 0)
        for entry in entries
    )
    total_protein = sum(
        float(entry.get("protein_g") or 0)
        for entry in entries
    )
    date_label = source_date.strftime("%a %b %d").replace(" 0", " ")
    lines = [
        "Yesterday's Food Review",
        "",
        date_label,
        *format_previous_food_entries(entries),
        "",
        "Total: "
        f"{format_display_number(total_calories, decimals=0)} calories, "
        f"{format_display_number(total_protein)} g protein",
        "",
        "1. Copy one meal",
        "2. Copy entire day",
        "3. Back",
        "",
        "Nothing has been copied yet.",
    ]
    return "\n".join(lines)


def format_yesterday_meal_choices(entries: list[dict]) -> str:
    grouped = {
        meal: [
            entry
            for entry in entries
            if entry.get("meal_category") == meal
        ]
        for meal in MEAL_DISPLAY_ORDER
    }
    available = [meal for meal in MEAL_DISPLAY_ORDER if grouped[meal]]
    lines = ["Choose a meal from yesterday to copy:", ""]
    for index, meal in enumerate(available, start=1):
        calories = sum(
            float(entry.get("calories") or 0)
            for entry in grouped[meal]
        )
        lines.append(
            f"{index}. {meal.title()} — {len(grouped[meal])} item(s), "
            f"{format_display_number(calories, decimals=0)} cal"
        )
    lines.extend(["", "Reply Back to return or Cancel to close."])
    return "\n".join(lines)


def format_yesterday_copy_confirmation(
    entries: list[dict],
    *,
    meal_category: str | None,
) -> str:
    selected = [
        entry
        for entry in entries
        if meal_category is None
        or entry.get("meal_category") == meal_category
    ]
    title = (
        "Copy this meal from yesterday?"
        if meal_category is not None
        else "Copy yesterday's food?"
    )
    calories = sum(
        float(entry.get("calories") or 0)
        for entry in selected
    )
    lines = [
        title,
        *format_previous_food_entries(selected),
        "",
        f"Items: {len(selected)}",
        "Calories to add today: "
        f"{format_display_number(calories, decimals=0)}",
        "",
        "Today's matching meal must be empty. This prevents an "
        "accidental duplicate copy.",
        "",
        "1. Yes, copy it",
        "2. No",
    ]
    return "\n".join(lines)


def format_edit_food_choices(entries: list[dict]) -> str:
    lines = ["Choose a food to edit:", ""]

    for index, entry in enumerate(entries, start=1):
        name = format_food_display_name(
            canonical_name=str(
                entry.get("canonical_name") or "Food"
            ),
            restaurant=entry.get("restaurant"),
            size=None,
        )
        lines.append(
            f"{index}. {name} — "
            f"{str(entry.get('meal_category') or 'Other').title()}, "
            f"quantity {format_display_number(float(entry.get('quantity') or 1))}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_save_favorite_choices(entries: list[dict]) -> str:
    lines = ["Choose a food to save as a favorite:", ""]

    for index, entry in enumerate(entries, start=1):
        name = format_food_display_name(
            canonical_name=str(entry.get("canonical_name") or "Food"),
            restaurant=entry.get("restaurant"),
            size=None,
        )
        lines.append(
            f"{index}. {name} — "
            f"{str(entry.get('meal_category') or 'Other').title()}, "
            f"quantity {format_display_number(float(entry.get('quantity') or 1))}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_favorite_choices(
    favorites: list[dict],
    *,
    action: str,
) -> str:
    heading = (
        "Choose a favorite to remove:"
        if action == "remove"
        else "Choose a favorite to log:"
    )
    lines = [heading, ""]

    for index, favorite in enumerate(favorites, start=1):
        name = format_food_display_name(
            canonical_name=str(
                favorite.get("canonical_name") or "Food"
            ),
            restaurant=favorite.get("restaurant"),
            size=None,
        )
        lines.append(
            f"{index}. {name} — "
            f"{str(favorite.get('meal_category') or 'Other').title()}, "
            f"quantity {format_display_number(float(favorite.get('quantity') or 1))}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_edit_food_action(entry: dict) -> str:
    name = format_food_display_name(
        canonical_name=str(entry.get("canonical_name") or "Food"),
        restaurant=entry.get("restaurant"),
        size=None,
    )
    return (
        f"Editing: {name}\n"
        f"Meal: {str(entry.get('meal_category') or 'Other').title()}\n"
        f"Quantity: {format_display_number(float(entry.get('quantity') or 1))}\n\n"
        "What would you like to change?\n"
        "1. Quantity\n"
        "2. Meal\n"
        "3. Back"
    )


def handle_food_photo_conversation(
    *,
    active_conversation: dict,
    text: str,
    chat_id: int | str,
) -> bool:
    current_step = active_conversation.get("current_step")
    known_data = dict(
        active_conversation.get("known_data") or {}
    )
    lowered = text.lower().strip()

    if lowered in {"cancel", "exit", "quit", "close"}:
        cancel_conversation(chat_id)
        send_telegram_msg(
            "Meal-photo estimate cancelled. Nothing was logged.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return True

    if current_step in {"clarification", "portion", "meal"}:
        estimate = dict(known_data.get("estimate") or {})
        if not estimate.get("readable"):
            start_conversation(
                chat_id=chat_id,
                conversation_type="healthcoach_menu",
                current_step="await_food_photo",
                known_data={},
                missing_fields=[],
                original_message="Retry unreadable meal photo",
            )
            send_telegram_msg(
                "I couldn't see enough nutrition information in "
                "that photo to continue safely. Nothing was "
                "logged.\n\nSend another clear meal photo, reply "
                "Back, or reply Cancel.",
                chat_id=chat_id,
            )
            return True

    if current_step == "clarification":
        estimate = dict(known_data.get("estimate") or {})

        if lowered not in {
            "skip",
            "skip details",
            "no details",
        }:
            send_telegram_msg(
                "Thanks. I'm refining the estimate with those "
                "details.",
                chat_id=chat_id,
            )

            try:
                estimate = refine_food_photo_estimate(
                    estimate,
                    user_details=text,
                )
            except Exception:
                logging.exception(
                    "Food-photo estimate refinement failed"
                )
                send_telegram_msg(
                    "I couldn't refine the estimate from that "
                    "description. Try different details, reply "
                    "Skip details, or Cancel.",
                    chat_id=chat_id,
                )
                return True

        update_conversation(
            chat_id=chat_id,
            current_step="portion",
            known_data={
                "estimate": estimate,
                "clarification": (
                    None
                    if lowered in {
                        "skip",
                        "skip details",
                        "no details",
                    }
                    else text
                ),
            },
            missing_fields=[],
        )

        send_telegram_msg(
            format_food_photo_estimate(estimate)
            + "\n\n"
            "How much of the pictured food did you eat?\n"
            "1. All of it\n"
            "2. About 3/4\n"
            "3. About half\n"
            "4. Cancel",
            chat_id=chat_id,
        )
        return True

    if current_step == "portion":
        portions = {
            "1": 1.0,
            "all": 1.0,
            "all of it": 1.0,
            "2": 0.75,
            "3/4": 0.75,
            "75%": 0.75,
            "about 3/4": 0.75,
            "3": 0.5,
            "half": 0.5,
            "1/2": 0.5,
            "50%": 0.5,
            "about half": 0.5,
        }

        portion_fraction = portions.get(lowered)

        if portion_fraction is None:
            send_telegram_msg(
                "Please choose All of it, About 3/4, "
                "About half, or Cancel.",
                chat_id=chat_id,
            )
            return True

        update_conversation(
            chat_id=chat_id,
            current_step="meal",
            known_data={
                "portion_fraction": portion_fraction,
            },
            missing_fields=[],
        )

        send_telegram_msg(
            "Which meal should this estimate be logged under?\n\n"
            "Before breakfast\n"
            "Breakfast\n"
            "Morning snack\n"
            "Lunch\n"
            "Afternoon snack\n"
            "Dinner\n"
            "Dessert",
            chat_id=chat_id,
        )
        return True

    if current_step == "meal":
        meal_aliases = {
            "before breakfast": "before breakfast",
            "breakfast": "breakfast",
            "morning snack": "school snack",
            "school snack": "school snack",
            "lunch": "lunch",
            "afternoon snack": "afternoon snack",
            "dinner": "dinner",
            "dessert": "dessert",
        }

        meal_category = meal_aliases.get(lowered)

        if meal_category is None:
            send_telegram_msg(
                "Please choose one of the displayed meal options.",
                chat_id=chat_id,
            )
            return True

        estimate = dict(known_data.get("estimate") or {})
        portion_fraction = float(
            known_data.get("portion_fraction") or 1.0
        )

        nutrition = midpoint_food_photo_nutrition(
            estimate,
            portion_fraction=portion_fraction,
        )

        dish_name = str(
            estimate.get("dish_name")
            or "Photo-estimated meal"
        ).strip()

        update_conversation(
            chat_id=chat_id,
            current_step="confirm",
            known_data={
                "meal_category": meal_category,
                "nutrition": nutrition,
                "dish_name": dish_name,
            },
            missing_fields=[],
        )

        send_telegram_msg(
            "Log this estimated meal?\n\n"
            f"Food: {dish_name}\n"
            f"Meal: {meal_category.title()}\n"
            f"Amount eaten: "
            f"{format_display_number(portion_fraction * 100, decimals=0)}%\n\n"
            "Estimated midpoint nutrition:\n"
            f"Calories: {format_display_number(nutrition['calories'], decimals=0)}\n"
            f"Protein: {format_display_number(nutrition['protein_g'])} g\n"
            f"Carbohydrates: "
            f"{format_display_number(nutrition['carbohydrates_g'])} g\n"
            f"Fat: {format_display_number(nutrition['fat_g'])} g\n\n"
            "This will be logged as a visual estimate.\n\n"
            "1. Log Estimate\n"
            "2. Change details\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return True

    if current_step == "confirm":
        if lowered in {
            "2",
            "change",
            "change details",
            "edit",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="clarification",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                "Tell me any details that would improve this "
                "estimate. Include the meat or protein, cooking "
                "method, and any oil, sauce, or dressing.\n\n"
                "Reply with details, Skip details, or Cancel.",
                chat_id=chat_id,
            )
            return True

        if lowered not in {
            "1",
            "yes",
            "log",
            "log estimate",
        }:
            send_telegram_msg(
                "Please choose Log Estimate, Change details, "
                "or Cancel.",
                chat_id=chat_id,
            )
            return True

        nutrition = dict(known_data.get("nutrition") or {})
        estimate = dict(known_data.get("estimate") or {})
        dish_name = str(
            known_data.get("dish_name")
            or "Photo-estimated meal"
        ).strip()
        meal_category = str(
            known_data.get("meal_category") or ""
        ).strip()

        timestamp = datetime.now(PACIFIC_TZ).isoformat(
            timespec="seconds"
        )

        try:
            created = add_food_with_nutrition(
                canonical_name=dish_name,
                serving_description=(
                    f"1 visual estimate {timestamp}"
                ),
                serving_amount=1.0,
                serving_unit="estimated meal",
                verification_status="estimated",
                verification_source="visual_estimate",
                calories=nutrition["calories"],
                protein_g=nutrition["protein_g"],
                carbohydrates_g=nutrition[
                    "carbohydrates_g"
                ],
                fat_g=nutrition["fat_g"],
                fiber_g=None,
                sugar_g=None,
                sodium_mg=None,
                food_type="meal",
            )

            entry = add_food_entry(
                entry_date=datetime.now(PACIFIC_TZ).date(),
                meal_category=meal_category,
                food_id=int(created["food"]["food_id"]),
                quantity=1.0,
                logging_source="telegram_ai",
                original_text=(
                    active_conversation.get("original_message")
                    or "Food photo"
                ),
                quantity_is_estimated=True,
                user_confirmed=True,
            )
        except Exception:
            logging.exception(
                "Food-photo estimated meal logging failed"
            )
            send_telegram_msg(
                "I couldn't log that estimate safely. Nothing "
                "was added to today's food log.",
                chat_id=chat_id,
            )
            return True

        complete_conversation(chat_id)

        send_telegram_msg(
            append_goal_calorie_progress(
                "Estimated meal logged.\n\n"
                f"Food: {dish_name}\n"
                f"Meal: {meal_category.title()}\n"
                f"Calories: "
                f"{format_display_number(float(entry['calories'] or 0), decimals=0)}\n"
                f"Protein: "
                f"{format_display_number(float(entry['protein_g'] or 0))} g\n\n"
                "This entry is marked as a visual estimate.",
                datetime.now(PACIFIC_TZ).date(),
            ),
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return True

    cancel_conversation(chat_id)
    send_telegram_msg(
        "That meal-photo estimate expired. Please send the "
        "photo again.",
        chat_id=chat_id,
        remove_keyboard=True,
    )
    return True


def process_telegram_update(update):
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()
    photo_sizes = list(message.get("photo") or [])

    if CHAT_ID and str(chat_id) != str(CHAT_ID):
        return

    if photo_sizes:
        file_id = photo_sizes[-1].get("file_id")
        photo_conversation = get_active_conversation(chat_id)
        photo_step = (
            photo_conversation.get("current_step")
            if photo_conversation
            and photo_conversation.get("conversation_type")
            == "healthcoach_menu"
            else None
        )

        if not file_id:
            send_telegram_msg(
                "I received the photo, but Telegram did not "
                "provide a usable file.",
                chat_id=chat_id,
            )
            return

        expected_photo_steps = {
            "await_menu_photo",
            "await_food_photo",
            "await_pantry_photo",
            "pantry_photo_result",
            "pantry_photo_review",
            "pantry_photo_remove",
            "pantry_photo_edit",
            "pantry_photo_edit_name",
            "await_recipe_photo",
            "recipe_import_input",
            "await_barcode_photo",
            "await_barcode_number",
            "barcode_teach_label_photo",
            "pantry_nutrition_label_photo",
        }
        caption_intent = photo_intent_from_caption(caption)

        if (
            photo_step not in expected_photo_steps
            and caption_intent is None
        ):
            start_conversation(
                chat_id=chat_id,
                conversation_type="healthcoach_menu",
                current_step="photo_intent",
                known_data={
                    "photo_file_id": file_id,
                    "photo_caption": caption,
                },
                missing_fields=["photo_intent"],
                original_message=caption or "Telegram photo",
            )
            send_telegram_msg(
                healthcoach_photo_intent_text(),
                chat_id=chat_id,
            )
            return

        if photo_step not in expected_photo_steps:
            intent_step = {
                "meal": "await_food_photo",
                "menu": "await_menu_photo",
                "barcode": "await_barcode_photo",
                "pantry_barcode": "await_barcode_photo",
                "pantry_photo": "await_pantry_photo",
                "recipe": "await_recipe_photo",
            }[caption_intent]
            start_conversation(
                chat_id=chat_id,
                conversation_type="healthcoach_menu",
                current_step=intent_step,
                known_data={
                    "pantry_scan_mode": (
                        caption_intent == "pantry_barcode"
                    ),
                    "pantry_photo_names": (
                        [] if caption_intent == "pantry_photo" else None
                    ),
                },
                missing_fields=[],
                original_message=caption or "Telegram photo",
            )
            photo_step = intent_step

        barcode_photo = (
            photo_step in {
                "await_barcode_photo",
                "await_barcode_number",
            }
        )

        label_photo = photo_step in {
            "barcode_teach_label_photo",
            "pantry_nutrition_label_photo",
        }

        food_photo = (
            photo_step == "await_food_photo"
            or (
                photo_step not in {
                    "await_menu_photo",
                    "await_barcode_photo",
                    "barcode_teach_label_photo",
                }
                and is_food_photo_request(caption)
            )
        )

        recipe_photo = photo_step in {
            "await_recipe_photo",
            "recipe_import_input",
        }

        pantry_photo = photo_step in {
            "await_pantry_photo",
            "pantry_photo_result",
            "pantry_photo_review",
            "pantry_photo_remove",
            "pantry_photo_edit",
            "pantry_photo_edit_name",
        }

        if label_photo:
            progress_message = (
                "I'm reading the Nutrition Facts label. "
                "This may take a moment."
            )
        elif barcode_photo:
            progress_message = (
                "I'm reading the barcode and checking the exact "
                "product in our food databases. This may take a moment."
            )
        elif recipe_photo:
            progress_message = (
                "I'm reading the recipe and organizing its ingredients. "
                "This may take a moment."
            )
        elif pantry_photo:
            progress_message = (
                "I'm checking the photo for clearly visible Pantry items. "
                "This may take a moment."
            )
        elif food_photo:
            progress_message = (
                "I'm examining the meal and estimating nutrition "
                "ranges. This may take a moment."
            )
        else:
            progress_message = (
                "I'm reading the menu photo and looking for up "
                "to three promising entrees. This may take a moment."
            )

        send_telegram_msg(
            progress_message,
            chat_id=chat_id,
        )

        try:
            image_bytes, mime_type = download_telegram_photo(
                telegram_token=TELEGRAM_TOKEN,
                file_id=file_id,
            )

            if label_photo:
                label_result = read_nutrition_label_photo(
                    image_bytes,
                    mime_type=mime_type,
                )

                if not label_result.get("readable"):
                    notes = list(
                        label_result.get("notes") or []
                    )
                    note_text = (
                        "\n".join(f"- {note}" for note in notes)
                        or "- One or more required values were unreadable."
                    )
                    response_message = (
                        "I couldn't read a complete Nutrition Facts "
                        "serving from that photo.\n\n"
                        f"{note_text}\n\n"
                        "Try another close, straight-on photo that "
                        "shows the serving size and all nutrient lines."
                    )
                elif photo_step == "pantry_nutrition_label_photo":
                    photo_known_data = dict(
                        (photo_conversation or {}).get("known_data") or {}
                    )
                    pantry_item = get_pantry_item_by_id(
                        int(
                            photo_known_data.get("pantry_nutrition_item_id")
                            or 0
                        )
                    )
                    if pantry_item is None:
                        response_message = (
                            "That Pantry item is no longer available. "
                            "Nothing was saved."
                        )
                    else:
                        update_conversation(
                            chat_id=chat_id,
                            current_step=(
                                "pantry_nutrition_label_confirmation"
                            ),
                            known_data={
                                **photo_known_data,
                                "pantry_nutrition_label_result": label_result,
                            },
                            missing_fields=[],
                        )
                        response_message = format_pantry_nutrition_review(
                            heading=(
                                "Use this Nutrition Facts label for the "
                                "Pantry item?"
                            ),
                            pantry_name=str(pantry_item["display_name"]),
                            food={
                                "canonical_name": pantry_item["display_name"],
                                "serving_description": label_result.get(
                                    "serving_description"
                                ),
                            },
                            nutrition=label_result,
                            source="Package Nutrition Facts label",
                        )
                else:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="barcode_teach_product_name",
                        known_data={
                            "barcode_label": label_result,
                        },
                        missing_fields=["product_name"],
                    )
                    suggested_name = str(
                        label_result.get("product_name") or ""
                    ).strip()
                    suggestion = (
                        f"\n\nThe photo may show: {suggested_name}"
                        if suggested_name
                        else ""
                    )
                    response_message = (
                        "I read the Nutrition Facts label. What should "
                        "this product be called in HealthCoach?"
                        f"{suggestion}\n\n"
                        "Example: Great Value Black Beans"
                    )
            elif barcode_photo:
                barcode_read = read_barcode_photo(
                    image_bytes,
                    mime_type=mime_type,
                )
                barcode = barcode_read.get("barcode")

                if not barcode_read.get("readable") or not barcode:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="await_barcode_number",
                        known_data={},
                        missing_fields=["barcode"],
                    )
                    response_message = (
                        "I couldn't confidently read the complete "
                        "barcode.\n\n"
                        "Type the barcode number printed beneath "
                        "the bars. Include the small digits on both "
                        "ends."
                    )
                else:
                    result = lookup_barcode_nutrition(
                        barcode
                    )

                    if result.get("found"):
                        update_conversation(
                            chat_id=chat_id,
                            current_step="barcode_result",
                            known_data={
                                "barcode": barcode,
                                "barcode_result": result,
                                "barcode_saved": bool(
                                    result.get("saved_food_id")
                                ),
                                "barcode_food_id": result.get(
                                    "saved_food_id"
                                ),
                            },
                            missing_fields=[],
                        )
                        response_message = (
                            format_barcode_product(
                                result,
                                barcode=barcode,
                                saved=bool(
                                    result.get("saved_food_id")
                                ),
                                pantry_item_name=(
                                    dict(
                                        (photo_conversation or {}).get(
                                            "known_data"
                                        )
                                        or {}
                                    ).get("pantry_nutrition_item_name")
                                ),
                            )
                        )
                    else:
                        update_conversation(
                            chat_id=chat_id,
                            current_step="barcode_teach_offer",
                            known_data={
                                "barcode": barcode,
                                "barcode_lookup_notes": list(
                                    result.get("notes") or []
                                ),
                            },
                            missing_fields=[],
                        )
                        notes = list(result.get("notes") or [])
                        note_text = (
                            "\n".join(f"- {note}" for note in notes)
                            or "- No exact usable USDA product was found."
                        )
                        response_message = (
                            f"I read barcode {barcode}, but couldn't "
                            "retrieve complete verified nutrition.\n\n"
                            f"{note_text}\n\n"
                            "Teach this barcode from the package label?\n\n"
                            "1. Teach from label\n"
                            "2. Try another barcode\n"
                            "3. Back\n"
                            "4. Cancel"
                        )

            elif recipe_photo:
                result = parse_recipe_photo(
                    image_bytes,
                    mime_type=mime_type,
                    user_context=caption,
                )
                start_recipe_import_draft(
                    chat_id=chat_id,
                    draft=result,
                )
                return
            elif pantry_photo:
                result = analyze_pantry_photo(
                    image_bytes,
                    mime_type=mime_type,
                    user_context=caption,
                )
                existing_names = list(
                    (photo_conversation or {}).get("known_data", {}).get(
                        "pantry_photo_names"
                    )
                    or []
                )
                detected_names = [
                    str(item.get("display_name") or "").strip()
                    for item in result.get("items") or []
                    if str(item.get("display_name") or "").strip()
                ]
                if not result.get("readable") or not detected_names:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="await_pantry_photo",
                        known_data={
                            "pantry_photo_names": existing_names,
                        },
                        missing_fields=["pantry_photo"],
                    )
                    response_message = (
                        "I couldn't identify any Pantry items clearly "
                        "enough to add from that photo.\n\n"
                        "Try a closer, well-lit photo with package fronts "
                        "facing the camera. Nothing was added."
                    )
                else:
                    combined_names = merge_pantry_photo_names(
                        existing_names,
                        detected_names,
                    )
                    new_names = combined_names[len(existing_names) :]
                    update_conversation(
                        chat_id=chat_id,
                        current_step="pantry_photo_result",
                        known_data={
                            "pantry_photo_names": combined_names,
                        },
                        missing_fields=[],
                    )
                    response_message = format_pantry_photo_result(
                        new_names,
                        combined_names,
                    )
            elif food_photo:
                result = analyze_food_photo(
                    image_bytes,
                    mime_type=mime_type,
                    user_context=caption,
                )

                if result.get("readable"):
                    start_conversation(
                        chat_id=chat_id,
                        conversation_type="food_photo_estimate",
                        current_step="clarification",
                        known_data={
                            "estimate": result,
                            "photo_caption": caption,
                        },
                        missing_fields=[],
                        original_message=caption or "Food photo",
                    )

                    response_message = (
                        format_food_photo_estimate(result)
                        + "\n\n"
                        "Tell me any details that would improve this "
                        "estimate. The most useful details are:\n"
                        "- Type of meat or protein\n"
                        "- Grilled, fried, breaded, or another method\n"
                        "- Added oil, sauce, or dressing\n\n"
                        "Reply with the details, Skip details, or Cancel."
                    )
                else:
                    start_conversation(
                        chat_id=chat_id,
                        conversation_type="healthcoach_menu",
                        current_step="await_food_photo",
                        known_data={},
                        missing_fields=[],
                        original_message="Retry unreadable meal photo",
                    )
                    response_message = (
                        format_food_photo_estimate(result)
                        + "\n\nNothing was logged. Send another clear "
                        "meal photo, reply Back, or reply Cancel."
                    )
            else:
                result = analyze_menu_photo(
                    image_bytes,
                    mime_type=mime_type,
                    user_context=caption,
                )
                response_message = format_menu_photo_analysis(
                    result
                )
        except Exception:
            logging.exception(
                "%s photo analysis failed",
                (
                    "Nutrition label"
                    if label_photo
                    else "Barcode"
                    if barcode_photo
                    else "Recipe"
                    if recipe_photo
                    else "Pantry"
                    if pantry_photo
                    else "Food"
                    if food_photo
                    else "Menu"
                ),
            )

            if label_photo:
                error_message = (
                    "I couldn't read that Nutrition Facts photo. "
                    "Try a closer, brighter picture showing the "
                    "serving size and every nutrient line."
                )
            elif barcode_photo:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_barcode_number",
                    known_data={},
                    missing_fields=["barcode"],
                )
                error_message = (
                    "I couldn't read that barcode photo. Try a "
                    "closer picture with the entire barcode in "
                    "focus, or type the number beneath the bars."
                )
            elif recipe_photo:
                error_message = (
                    "I couldn't read that recipe photo reliably. Try a "
                    "closer picture showing the full ingredient list, or "
                    "paste the recipe text. Nothing was saved."
                )
            elif pantry_photo:
                pending_names = list(
                    (photo_conversation or {}).get("known_data", {}).get(
                        "pantry_photo_names"
                    )
                    or []
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_pantry_photo",
                    known_data={"pantry_photo_names": pending_names},
                    missing_fields=["pantry_photo"],
                )
                error_message = (
                    "I couldn't analyze that Pantry photo. Try a closer, "
                    "well-lit picture with package fronts visible. Nothing "
                    "was added."
                )
            elif food_photo:
                error_message = (
                    "I couldn't estimate that meal photo. Try a "
                    "brighter picture showing the entire plate."
                )
            else:
                error_message = (
                    "I couldn't analyze that menu photo. Try a "
                    "closer, brighter picture with the item names "
                    "and descriptions in focus."
                )

            send_telegram_msg(
                error_message,
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            response_message,
            chat_id=chat_id,
        )
        return

    if not text:
        return

    if text == "/status":
        metrics = get_today_metrics()
        send_telegram_msg(
            build_progress_message("Current status", metrics),
            chat_id=chat_id,
        )
        return

    lowered_text = text.lower().strip()

    if lowered_text in {"/menu", "menu", "main menu"}:
        start_conversation(
            chat_id=chat_id,
            conversation_type="healthcoach_menu",
            current_step="main",
            known_data={},
            missing_fields=[],
            original_message=text,
        )
        send_telegram_msg(
            healthcoach_main_menu_text(),
            chat_id=chat_id,
        )
        return

    if lowered_text in {
        "update unknown foods",
        "/updateunknownfoods",
        "update unresolved foods",
    }:
        pending = get_pending_unresolved_foods()

        if not pending:
            send_telegram_msg(
                "There are no unknown foods waiting for nutrition.",
                chat_id=chat_id,
            )
            return

        show_unresolved_food_review(
            chat_id=chat_id,
            pending=pending,
        )
        return

    active_conversation = get_active_conversation(chat_id)
    forced_food_entry_date = None

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and lowered_text in {"cancel", "exit", "quit", "close"}
    ):
        cancel_conversation(chat_id)
        send_telegram_msg(
            "Food entry cancelled. Nothing was logged.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "yesterday_food_logging"
        and active_conversation.get("current_step")
        == "awaiting_food"
    ):
        lowered = text.lower().strip()
        if lowered in {"cancel", "exit", "quit", "close"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Yesterday's food entry was cancelled.",
                chat_id=chat_id,
                remove_keyboard=True,
            )
            return

        if lowered in {"back", "food menu"}:
            start_conversation(
                chat_id=chat_id,
                conversation_type="healthcoach_menu",
                current_step="food",
                known_data={},
                missing_fields=[],
                original_message="",
            )
            send_telegram_msg(
                healthcoach_food_menu_text(),
                chat_id=chat_id,
            )
            return

        expected_yesterday = (
            datetime.now(PACIFIC_TZ).date()
            - timedelta(days=1)
        )
        stored_date = parse_food_entry_date(
            (active_conversation.get("known_data") or {}).get(
                "_entry_date"
            )
        )
        forced_food_entry_date = (
            stored_date
            if stored_date == expected_yesterday
            else expected_yesterday
        )
        cancel_conversation(chat_id)
        active_conversation = None

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_photo_estimate"
    ):
        if handle_food_photo_conversation(
            active_conversation=active_conversation,
            text=text,
            chat_id=chat_id,
        ):
            return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "healthcoach_menu"
    ):
        current_step = active_conversation.get("current_step")
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        lowered = text.lower().strip()
        today = datetime.now(PACIFIC_TZ).date()

        if lowered in {"cancel", "exit", "quit", "close"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Menu closed.",
                chat_id=chat_id,
                remove_keyboard=True,
            )
            return

        if lowered in {"menu", "main", "main menu"}:
            update_conversation(
                chat_id=chat_id,
                current_step="main",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                healthcoach_main_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "main":
            if lowered in {"1", "food"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "health"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_health_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "reports"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="reports",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_reports_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "help"}:
                send_telegram_msg(
                    "You can still use natural language at any time "
                    "after closing the menu.\n\n"
                    "Examples:\n"
                    "- For lunch I had...\n"
                    "- Record my sleep as 7:15\n"
                    "- Run my weekly report\n\n"
                    "Reply menu to return or cancel to close.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_main_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "food":
            if lowered in {"1", "log", "log food"}:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Send the food naturally, including the meal.\n\n"
                    "Example: For lunch I had a turkey sandwich.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "2",
                "log food for yesterday",
                "log yesterday",
                "add food to yesterday",
            }:
                entry_date = today - timedelta(days=1)
                start_conversation(
                    chat_id=chat_id,
                    conversation_type="yesterday_food_logging",
                    current_step="awaiting_food",
                    known_data={
                        "_entry_date": entry_date.isoformat(),
                    },
                    missing_fields=[],
                    original_message="",
                )
                send_telegram_msg(
                    "Logging food for yesterday — "
                    f"{entry_date.strftime('%a %b')} "
                    f"{entry_date.day}, {entry_date.year}.\n\n"
                    "Send the food naturally, including the meal.\n\n"
                    "Example: For lunch I had a turkey sandwich.\n\n"
                    "Reply Back to return or Cancel to close.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "show", "show today"}:
                send_telegram_msg(
                    format_daily_food_log(today),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "edit", "edit today", "edit today's food"}:
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda entry: int(entry["food_entry_id"]),
                    reverse=True,
                )

                if not entries:
                    send_telegram_msg(
                        "There are no food entries to edit today.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_select",
                    known_data={
                        "_edit_food_entry_ids": [
                            int(entry["food_entry_id"])
                            for entry in entries
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_edit_food_choices(entries),
                    chat_id=chat_id,
                )
                return

            if lowered in {"5", "undo", "undo last"}:
                entries = list_food_entries(entry_date=today)

                if not entries:
                    send_telegram_msg(
                        "There is no food entry to undo today.",
                        chat_id=chat_id,
                    )
                    return

                latest = max(
                    entries,
                    key=lambda entry: int(
                        entry["food_entry_id"]
                    ),
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="undo_food_confirmation",
                    known_data={
                        "_undo_food_entry_id": (
                            latest["food_entry_id"]
                        ),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Undo this food entry?\n\n"
                    f"Meal: {str(latest['meal_category']).title()}\n"
                    f"Food: {latest['canonical_name']}\n"
                    f"Calories: {format_display_number(float(latest.get('calories') or 0), decimals=0)}\n\n"
                    "1. Yes, undo it\n"
                    "2. No, keep it",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "6",
                "same as yesterday",
                "yesterday",
                "copy yesterday",
            }:
                source_date = today - timedelta(days=1)
                entries = list_food_entries(entry_date=source_date)
                if not entries:
                    send_telegram_msg(
                        "No foods were recorded yesterday, so there "
                        "is nothing to copy.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="same_yesterday_review",
                    known_data={
                        "same_yesterday_source_date": (
                            source_date.isoformat()
                        ),
                        "same_yesterday_meal": None,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_yesterday_food_review(
                        entries,
                        source_date=source_date,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"7", "favorites", "favorite"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorites",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_favorites_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "8",
                "food library",
                "library",
                "saved foods",
                "saved food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "9",
                "saved recipes",
                "saved recipe",
                "recipes",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "10",
                "my pantry",
                "pantry",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "11",
                "photo",
                "photo tools",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="photo_tools",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_photo_tools_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "12",
                "restaurant",
                "restaurant choices",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="restaurant",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_restaurant_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "13",
                "unknown",
                "update unknown foods",
            }:
                pending = get_pending_unresolved_foods()
                if not pending:
                    send_telegram_msg(
                        "There are no unknown foods waiting.",
                        chat_id=chat_id,
                    )
                else:
                    show_unresolved_food_review(
                        chat_id=chat_id,
                        pending=pending,
                    )
                return

            if lowered in {"14", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="main",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_main_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_food_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "same_yesterday_review":
            source_value = str(
                known_data.get("same_yesterday_source_date") or ""
            )
            try:
                source_date = datetime.fromisoformat(
                    source_value
                ).date()
            except ValueError:
                source_date = today - timedelta(days=1)
            entries = list_food_entries(entry_date=source_date)

            if not entries:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Yesterday's food is no longer available to copy.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"1", "copy one meal", "one meal"}:
                available_meals = [
                    meal
                    for meal in MEAL_DISPLAY_ORDER
                    if any(
                        entry.get("meal_category") == meal
                        for entry in entries
                    )
                ]
                update_conversation(
                    chat_id=chat_id,
                    current_step="same_yesterday_meal_select",
                    known_data={
                        "same_yesterday_available_meals": (
                            available_meals
                        ),
                        "same_yesterday_meal": None,
                    },
                    missing_fields=["meal_category"],
                )
                send_telegram_msg(
                    format_yesterday_meal_choices(entries),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "copy entire day",
                "entire day",
                "copy all",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="same_yesterday_confirmation",
                    known_data={"same_yesterday_meal": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_yesterday_copy_confirmation(
                        entries,
                        meal_category=None,
                    ),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_yesterday_food_review(
                    entries,
                    source_date=source_date,
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "same_yesterday_meal_select":
            source_value = str(
                known_data.get("same_yesterday_source_date") or ""
            )
            try:
                source_date = datetime.fromisoformat(
                    source_value
                ).date()
            except ValueError:
                source_date = today - timedelta(days=1)
            entries = list_food_entries(entry_date=source_date)

            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="same_yesterday_review",
                    known_data={"same_yesterday_meal": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_yesterday_food_review(
                        entries,
                        source_date=source_date,
                    ),
                    chat_id=chat_id,
                )
                return

            available_meals = list(
                known_data.get("same_yesterday_available_meals")
                or []
            )
            try:
                selected_index = int(text.strip()) - 1
                if selected_index < 0:
                    raise IndexError
                meal_category = str(
                    available_meals[selected_index]
                )
            except (IndexError, TypeError, ValueError):
                send_telegram_msg(
                    format_yesterday_meal_choices(entries),
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="same_yesterday_confirmation",
                known_data={"same_yesterday_meal": meal_category},
                missing_fields=[],
            )
            send_telegram_msg(
                format_yesterday_copy_confirmation(
                    entries,
                    meal_category=meal_category,
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "same_yesterday_confirmation":
            source_value = str(
                known_data.get("same_yesterday_source_date") or ""
            )
            try:
                source_date = datetime.fromisoformat(
                    source_value
                ).date()
            except ValueError:
                source_date = today - timedelta(days=1)
            meal_category = known_data.get("same_yesterday_meal")
            entries = list_food_entries(entry_date=source_date)

            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="same_yesterday_review",
                    known_data={"same_yesterday_meal": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Nothing was copied.\n\n"
                    + format_yesterday_food_review(
                        entries,
                        source_date=source_date,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered not in {
                "1",
                "yes",
                "yes copy it",
                "copy it",
                "copy",
            }:
                send_telegram_msg(
                    format_yesterday_copy_confirmation(
                        entries,
                        meal_category=meal_category,
                    ),
                    chat_id=chat_id,
                )
                return

            try:
                copied = copy_food_entries_to_date(
                    source_date=source_date,
                    target_date=today,
                    meal_category=meal_category,
                )
            except ValueError as error:
                update_conversation(
                    chat_id=chat_id,
                    current_step="same_yesterday_review",
                    known_data={"same_yesterday_meal": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    str(error)
                    + "\n\n"
                    + format_yesterday_food_review(
                        entries,
                        source_date=source_date,
                    ),
                    chat_id=chat_id,
                )
                return
            except Exception:
                logging.exception("Same as yesterday copy failed")
                send_telegram_msg(
                    "I couldn't copy yesterday's food safely. "
                    "Nothing was added.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(today)
            except Exception:
                logging.exception(
                    "Same as yesterday Google Sheet sync failed"
                )
                sync_note = (
                    "\n\nThe food was copied, but the Google Sheet "
                    "totals could not be updated."
                )
            else:
                sync_note = ""

            copied_calories = sum(
                float(entry.get("calories") or 0)
                for entry in copied
            )
            update_conversation(
                chat_id=chat_id,
                current_step="food",
                known_data={
                    "same_yesterday_source_date": None,
                    "same_yesterday_meal": None,
                },
                missing_fields=[],
            )
            scope = (
                str(meal_category).title()
                if meal_category
                else "Yesterday's recorded day"
            )
            send_telegram_msg(
                "Copied food to today.\n\n"
                f"Copied: {scope}\n"
                f"Items: {len(copied)}\n"
                "Calories added: "
                + append_goal_calorie_progress(
                    f"{format_display_number(copied_calories, decimals=0)}"
                    + sync_note,
                    today,
                )
                + "\n\n"
                + healthcoach_food_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry":
            if lowered in {"1", "view", "view pantry"}:
                show_pantry_view_page(chat_id=chat_id)
                return

            if lowered in {
                "2",
                "add",
                "add pantry items",
                "add items",
                "add items manually",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_add_items",
                    known_data={"pantry_pending_names": []},
                    missing_fields=["pantry_items"],
                )
                send_telegram_msg(
                    "Send the Pantry items as a comma-separated list.\n\n"
                    "Example: chicken breast, romaine, tomatoes, "
                    "rice, black beans\n\n"
                    "Quantities are not needed.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "3",
                "scan product into pantry",
                "scan into pantry",
                "scan pantry item",
                "scan product",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_barcode_photo",
                    known_data={"pantry_scan_mode": True},
                    missing_fields=["barcode_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of a product barcode to add "
                    "to My Pantry.\n\n"
                    "Keep the entire barcode and the small digits on "
                    "both ends visible. You can also type the barcode "
                    "number instead.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "4",
                "add items from shelf photo",
                "add shelf photo",
                "pantry shelf photo",
                "shelf photo",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_pantry_photo",
                    known_data={"pantry_photo_names": []},
                    missing_fields=["pantry_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of one Pantry shelf.\n\n"
                    "Face package fronts toward the camera when possible. "
                    "HealthCoach will identify only clearly visible foods "
                    "and let you review everything before adding it.\n\n"
                    "You can combine more than one photo before confirming. "
                    "Quantities and nutrition are not inferred.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "5",
                "organize",
                "organize pantry",
                "organize pantry items",
            }:
                show_pantry_organizer_page(chat_id=chat_id)
                return

            if lowered in {
                "6",
                "complete pantry nutrition",
                "complete nutrition",
                "pantry nutrition",
            }:
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    skipped_ids=[],
                )
                return

            if lowered in {
                "7",
                "get meal ideas",
                "meal ideas",
                "pantry meal ideas",
            }:
                items = list_pantry_items()
                if not items:
                    send_telegram_msg(
                        "Your Pantry is empty. Add a few available "
                        "foods or scan products first.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_additional_choice",
                    known_data={
                        "pantry_meal_allow_additional": None,
                        "pantry_meal_type": None,
                        "pantry_meal_ideas": [],
                        "pantry_meal_selected_index": None,
                        "pantry_meal_servings": None,
                    },
                    missing_fields=["allow_additional_ingredients"],
                )
                send_telegram_msg(
                    pantry_meal_additional_ingredients_prompt(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "8",
                "smart pantry swaps",
                "smart pantry swap",
                "pantry swaps",
                "healthier swaps",
            }:
                show_smart_pantry_swaps(chat_id=chat_id)
                return

            if lowered in {
                "9",
                "shopping list",
                "my shopping list",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "10",
                "remove",
                "remove pantry item",
            }:
                show_pantry_remove_page(chat_id=chat_id)
                return

            if lowered in {
                "11",
                "clear",
                "clear pantry",
            }:
                items = list_pantry_items()
                if not items:
                    send_telegram_msg(
                        "Your Pantry is already empty.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_clear_confirmation",
                    known_data={
                        "pantry_clear_count": len(items),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Clear My Pantry?\n\n"
                    f"This will remove all {len(items)} available "
                    "items. Saved Foods and food logs will not change.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            if lowered in {"12", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_pantry_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "await_pantry_photo":
            if lowered == "back":
                pending_names = list(
                    known_data.get("pantry_photo_names") or []
                )
                if pending_names:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="pantry_photo_review",
                        known_data={
                            "pantry_photo_names": pending_names,
                        },
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        format_pantry_photo_review(pending_names),
                        chat_id=chat_id,
                    )
                else:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="pantry",
                        known_data={"pantry_photo_names": []},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        healthcoach_pantry_menu_text(),
                        chat_id=chat_id,
                    )
                return
            send_telegram_msg(
                "Send a clear photo of one Pantry shelf, or reply Back.",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_photo_result":
            names = list(known_data.get("pantry_photo_names") or [])
            if lowered in {"add another", "add another photo", "another"}:
                if len(names) >= MAX_PANTRY_PHOTO_SESSION_ITEMS:
                    send_telegram_msg(
                        "This review already has 30 distinct items. Review "
                        "and add them before starting another photo batch.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_pantry_photo",
                    known_data={"pantry_photo_names": names},
                    missing_fields=["pantry_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of another Pantry shelf. The new "
                    "items will be combined with your pending review.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"review", "review items"}:
                if not names:
                    send_telegram_msg(
                        "No Pantry items are pending. Send another clear "
                        "shelf photo or reply Cancel.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_review",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_review(names),
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                format_pantry_photo_result([], names),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_photo_review":
            names = list(known_data.get("pantry_photo_names") or [])
            if lowered in {"add another", "add another photo", "another"}:
                if len(names) >= MAX_PANTRY_PHOTO_SESSION_ITEMS:
                    send_telegram_msg(
                        "This review already has 30 distinct items. Add or "
                        "remove some before starting another photo batch.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_pantry_photo",
                    known_data={"pantry_photo_names": names},
                    missing_fields=["pantry_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of another Pantry shelf. The new "
                    "items will be combined with this review.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"remove", "remove item", "remove an item"}:
                if not names:
                    send_telegram_msg(
                        "No pending items can be removed.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_remove",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_remove_choices(names),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "edit",
                "edit item",
                "edit item name",
                "rename",
            }:
                if not names:
                    send_telegram_msg(
                        "No pending items can be edited.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_edit",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_edit_choices(names),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "add",
                "add all",
                "add all to pantry",
                "add to pantry",
            }:
                if not names:
                    send_telegram_msg(
                        "No Pantry items are pending.",
                        chat_id=chat_id,
                    )
                    return
                try:
                    result = add_pantry_items(
                        names,
                        source="shelf_photo",
                    )
                except Exception:
                    logging.exception(
                        "Could not add shelf-photo Pantry items"
                    )
                    send_telegram_msg(
                        "I couldn't add those Pantry items. Nothing was "
                        "changed.",
                        chat_id=chat_id,
                    )
                    return
                created_count = len(result["created"])
                existing_count = len(result["existing"])
                summary = (
                    f"Added {created_count} shelf-photo item(s) to My Pantry."
                )
                if existing_count:
                    summary += (
                        f" {existing_count} item(s) were already there."
                    )
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={"pantry_photo_names": []},
                    missing_fields=[],
                )
                send_telegram_msg(
                    summary
                    + "\n\nPhoto names are presence-only; no nutrition "
                    "was invented or logged.\n\n"
                    + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                format_pantry_photo_review(names),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_photo_remove":
            names = list(known_data.get("pantry_photo_names") or [])
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_review",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_review(names),
                    chat_id=chat_id,
                )
                return
            try:
                selected_index = int(text.strip()) - 1
            except (TypeError, ValueError):
                selected_index = -1
            if selected_index < 0 or selected_index >= len(names):
                send_telegram_msg(
                    format_pantry_photo_remove_choices(names),
                    chat_id=chat_id,
                )
                return
            removed_name = names.pop(selected_index)
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_photo_review",
                known_data={"pantry_photo_names": names},
                missing_fields=[],
            )
            if names:
                send_telegram_msg(
                    f"Removed {removed_name} from the pending review.\n\n"
                    + format_pantry_photo_review(names),
                    chat_id=chat_id,
                )
            else:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_pantry_photo",
                    known_data={"pantry_photo_names": []},
                    missing_fields=["pantry_photo"],
                )
                send_telegram_msg(
                    f"Removed {removed_name}. No items remain pending.\n\n"
                    "Send another clear Pantry shelf photo or reply Back.",
                    chat_id=chat_id,
                )
            return

        if current_step == "pantry_photo_edit":
            names = list(known_data.get("pantry_photo_names") or [])
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_review",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_review(names),
                    chat_id=chat_id,
                )
                return
            try:
                selected_index = int(text.strip()) - 1
            except (TypeError, ValueError):
                selected_index = -1
            if selected_index < 0 or selected_index >= len(names):
                send_telegram_msg(
                    format_pantry_photo_edit_choices(names),
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_photo_edit_name",
                known_data={
                    "pantry_photo_names": names,
                    "pantry_photo_edit_index": selected_index,
                },
                missing_fields=["pantry_photo_item_name"],
            )
            send_telegram_msg(
                f"Current name: {names[selected_index]}\n\n"
                "Type the corrected Pantry item name.",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_photo_edit_name":
            names = list(known_data.get("pantry_photo_names") or [])
            try:
                selected_index = int(
                    known_data.get("pantry_photo_edit_index")
                )
            except (TypeError, ValueError):
                selected_index = -1
            if selected_index < 0 or selected_index >= len(names):
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_review",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_review(names),
                    chat_id=chat_id,
                )
                return
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_photo_edit",
                    known_data={"pantry_photo_names": names},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_photo_edit_choices(names),
                    chat_id=chat_id,
                )
                return
            try:
                corrected_name = clean_pantry_name(text)
            except ValueError as exc:
                send_telegram_msg(
                    f"That name could not be used: {exc}\n\n"
                    "Type the corrected Pantry item name.",
                    chat_id=chat_id,
                )
                return
            previous_name = names[selected_index]
            names[selected_index] = corrected_name
            revised_names = merge_pantry_photo_names([], names)
            duplicate_merged = len(revised_names) < len(names)
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_photo_review",
                known_data={"pantry_photo_names": revised_names},
                missing_fields=[],
            )
            notice = f"Changed {previous_name} to {corrected_name}."
            if duplicate_merged:
                notice += " The duplicate pending item was merged."
            send_telegram_msg(
                notice + "\n\n" + format_pantry_photo_review(revised_names),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_swaps":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "refresh",
                "refresh swaps",
                "more",
                "more swaps",
            }:
                show_smart_pantry_swaps(chat_id=chat_id)
                return

            if lowered in {"shopping list", "my shopping list"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return

            match = re.fullmatch(r"add\s+([1-9][0-9]*)", lowered)
            if match:
                swaps = list(known_data.get("pantry_swaps") or [])
                selection = int(match.group(1))
                if selection > len(swaps):
                    send_telegram_msg(
                        "Choose one of the displayed swap numbers.",
                        chat_id=chat_id,
                    )
                    return

                swap = swaps[selection - 1]
                available_name = str(
                    swap.get("available_pantry_item_name") or ""
                ).strip()
                if available_name:
                    send_telegram_msg(
                        f"{available_name} is already in My Pantry, so "
                        "it was not added to the Shopping List.\n\n"
                        + format_smart_pantry_swaps(swaps),
                        chat_id=chat_id,
                    )
                    return

                replacement = str(
                    swap.get("suggested_replacement") or ""
                ).strip()
                try:
                    item = add_shopping_item(
                        display_name=replacement,
                        source="pantry_swap",
                        source_note=(
                            "Swap for "
                            + str(swap.get("pantry_item_name") or "item")
                        ),
                    )
                except Exception:
                    logging.exception(
                        "Could not add Smart Pantry Swap to Shopping List"
                    )
                    send_telegram_msg(
                        "I couldn't add that swap to the Shopping List. "
                        "Nothing was changed.",
                        chat_id=chat_id,
                    )
                    return

                status = (
                    f"Added {item['display_name']} to the Shopping List."
                    if item.get("created")
                    else f"{item['display_name']} is already on the "
                    "Shopping List."
                )
                send_telegram_msg(
                    status + "\n\n" + format_smart_pantry_swaps(swaps),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_smart_pantry_swaps(
                    list(known_data.get("pantry_swaps") or [])
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_meal_additional_choice":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "1",
                "no",
                "pantry only",
                "pantry only no shopping",
                "no shopping",
            }:
                allow_additional = False
            elif lowered in {
                "2",
                "yes",
                "up to 2 extra ingredients",
                "up to two extra ingredients",
                "extra ingredients",
            }:
                allow_additional = True
            else:
                send_telegram_msg(
                    pantry_meal_additional_ingredients_prompt(),
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="pantry_meal_type",
                known_data={
                    "pantry_meal_allow_additional": allow_additional,
                    "pantry_meal_type": None,
                },
                missing_fields=["meal_type"],
            )
            send_telegram_msg(
                pantry_meal_type_prompt(),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_meal_type":
            allow_additional = bool(
                known_data.get("pantry_meal_allow_additional", True)
            )
            if known_data.get("pantry_meal_allow_additional") is False:
                allow_additional = False

            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_additional_choice",
                    known_data={
                        "pantry_meal_allow_additional": None,
                        "pantry_meal_type": None,
                    },
                    missing_fields=["allow_additional_ingredients"],
                )
                send_telegram_msg(
                    pantry_meal_additional_ingredients_prompt(),
                    chat_id=chat_id,
                )
                return

            meal_type = (
                "lunch"
                if lowered in {"1", "lunch"}
                else "dinner"
                if lowered in {"2", "dinner"}
                else None
            )
            if meal_type is None:
                send_telegram_msg(
                    pantry_meal_type_prompt(),
                    chat_id=chat_id,
                )
                return

            if not show_pantry_meal_ideas(
                chat_id=chat_id,
                meal_type=meal_type,
                allow_additional_ingredients=allow_additional,
            ):
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_type",
                    known_data={
                        "pantry_meal_type": meal_type,
                        "pantry_meal_allow_additional": allow_additional,
                    },
                    missing_fields=["meal_type"],
                )
            return

        if current_step == "pantry_meal_ideas":
            ideas = list(
                known_data.get("pantry_meal_ideas") or []
            )
            meal_type = str(
                known_data.get("pantry_meal_type") or "dinner"
            ).lower()
            allow_additional = bool(
                known_data.get("pantry_meal_allow_additional", True)
            )
            if known_data.get("pantry_meal_allow_additional") is False:
                allow_additional = False

            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"more", "more ideas", "new ideas"}:
                show_pantry_meal_ideas(
                    chat_id=chat_id,
                    meal_type=meal_type,
                    allow_additional_ingredients=allow_additional,
                )
                return

            try:
                selected_index = int(text.strip()) - 1
            except (TypeError, ValueError):
                selected_index = -1

            if not 0 <= selected_index < len(ideas):
                send_telegram_msg(
                    format_pantry_meal_ideas(
                        ideas,
                        meal_type=meal_type,
                        goal_context=known_data.get(
                            "pantry_meal_goal_context"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return

            idea = ideas[selected_index]
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_meal_idea_details",
                known_data={
                    "pantry_meal_selected_index": selected_index,
                    "pantry_meal_servings": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_pantry_meal_idea_details(
                    idea,
                    meal_type=meal_type,
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_meal_idea_details":
            ideas = list(
                known_data.get("pantry_meal_ideas") or []
            )
            meal_type = str(
                known_data.get("pantry_meal_type") or "dinner"
            ).lower()
            allow_additional = bool(
                known_data.get("pantry_meal_allow_additional", True)
            )
            if known_data.get("pantry_meal_allow_additional") is False:
                allow_additional = False
            try:
                selected_index = int(
                    known_data.get("pantry_meal_selected_index")
                )
                idea = ideas[selected_index]
            except (IndexError, TypeError, ValueError):
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Those meal ideas are no longer available.\n\n"
                    + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_ideas",
                    known_data={"pantry_meal_servings": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_meal_ideas(
                        ideas,
                        meal_type=meal_type,
                        goal_context=known_data.get(
                            "pantry_meal_goal_context"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"more", "more ideas", "new ideas"}:
                show_pantry_meal_ideas(
                    chat_id=chat_id,
                    meal_type=meal_type,
                    allow_additional_ingredients=allow_additional,
                )
                return

            if lowered in {
                "save",
                "save recipe",
                "save this recipe",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_save_confirmation",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Save this recipe?\n\n"
                    f"Recipe: {idea.get('name') or 'Pantry meal'}\n"
                    f"Meal type: {meal_type.title()}\n"
                    "Nutrition, ingredients, and preparation will be "
                    "kept in Saved Recipes. Nothing will be logged.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "log",
                "log meal",
                "log it",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_servings",
                    known_data={"pantry_meal_servings": None},
                    missing_fields=["servings"],
                )
                send_telegram_msg(
                    "How many servings of this Pantry meal did you "
                    "eat?\n\nChoose 0.5, 1, 1.5, or 2.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_pantry_meal_idea_details(
                    idea,
                    meal_type=meal_type,
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_meal_save_confirmation":
            ideas = list(
                known_data.get("pantry_meal_ideas") or []
            )
            meal_type = str(
                known_data.get("pantry_meal_type") or "dinner"
            ).lower()
            try:
                selected_index = int(
                    known_data.get("pantry_meal_selected_index")
                )
                idea = ideas[selected_index]
            except (IndexError, TypeError, ValueError):
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That meal idea expired. Nothing was saved.\n\n"
                    + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_idea_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Recipe not saved.\n\n"
                    + format_pantry_meal_idea_details(
                        idea,
                        meal_type=meal_type,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            try:
                saved = save_pantry_meal_idea(
                    idea,
                    meal_type=meal_type,
                )
            except Exception:
                logging.exception("Saving Pantry recipe failed")
                send_telegram_msg(
                    "I couldn't save that recipe safely. Nothing "
                    "was added to Saved Recipes.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="pantry_meal_idea_details",
                known_data={},
                missing_fields=[],
            )
            outcome = (
                "Saved this recipe to Saved Recipes."
                if saved.get("created")
                else "This recipe is already in Saved Recipes."
            )
            send_telegram_msg(
                outcome + " Nothing was logged.\n\n"
                + format_pantry_meal_idea_details(
                    idea,
                    meal_type=meal_type,
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_meal_servings":
            ideas = list(
                known_data.get("pantry_meal_ideas") or []
            )
            meal_type = str(
                known_data.get("pantry_meal_type") or "dinner"
            ).lower()
            try:
                selected_index = int(
                    known_data.get("pantry_meal_selected_index")
                )
                idea = ideas[selected_index]
            except (IndexError, TypeError, ValueError):
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_idea_details",
                    known_data={"pantry_meal_servings": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_meal_idea_details(
                        idea,
                        meal_type=meal_type,
                    ),
                    chat_id=chat_id,
                )
                return

            try:
                servings = float(text.strip())
            except (TypeError, ValueError):
                servings = 0

            if servings <= 0 or servings > 4:
                send_telegram_msg(
                    "Enter a serving amount greater than 0 and no "
                    "more than 4. For example: 0.5, 1, 1.5, or 2.",
                    chat_id=chat_id,
                )
                return

            nutrition = scale_pantry_meal_nutrition(
                idea,
                servings=servings,
            )
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_meal_log_confirmation",
                known_data={"pantry_meal_servings": servings},
                missing_fields=[],
            )
            send_telegram_msg(
                "Log this Pantry meal estimate?\n\n"
                f"Food: {idea.get('name') or 'Pantry meal'}\n"
                f"Meal: {meal_type.title()}\n"
                f"Servings eaten: {format_display_number(servings)}\n\n"
                "Estimated nutrition to log:\n"
                "Calories: "
                f"{format_display_number(nutrition['calories'], decimals=0)}\n"
                "Protein: "
                f"{format_display_number(nutrition['protein_g'])} g\n"
                "Carbohydrates: "
                f"{format_display_number(nutrition['carbohydrates_g'])} g\n"
                "Fat: "
                f"{format_display_number(nutrition['fat_g'])} g\n\n"
                "Only log this after you have eaten it.\n\n"
                "1. Log Meal\n"
                "2. Back\n"
                "3. Cancel",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_meal_log_confirmation":
            ideas = list(
                known_data.get("pantry_meal_ideas") or []
            )
            meal_type = str(
                known_data.get("pantry_meal_type") or "dinner"
            ).lower()
            try:
                selected_index = int(
                    known_data.get("pantry_meal_selected_index")
                )
                idea = ideas[selected_index]
                servings = float(
                    known_data.get("pantry_meal_servings")
                )
            except (IndexError, TypeError, ValueError):
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "back", "change amount"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_meal_servings",
                    known_data={"pantry_meal_servings": None},
                    missing_fields=["servings"],
                )
                send_telegram_msg(
                    "How many servings of this Pantry meal did you "
                    "eat?\n\nChoose 0.5, 1, 1.5, or 2.",
                    chat_id=chat_id,
                )
                return

            if lowered not in {
                "1",
                "log",
                "log meal",
                "log it",
            }:
                send_telegram_msg(
                    "Reply Log Meal, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return

            timestamp = datetime.now(PACIFIC_TZ).strftime(
                "%Y%m%d-%H%M%S-%f"
            )
            try:
                created = add_food_with_nutrition(
                    canonical_name=str(
                        idea.get("name") or "Pantry meal"
                    ),
                    serving_description=(
                        f"1 Pantry estimate {timestamp}"
                    ),
                    serving_amount=1.0,
                    serving_unit="estimated serving",
                    verification_status="estimated",
                    verification_source="pantry_meal_idea",
                    calories=float(idea.get("calories") or 0),
                    protein_g=float(idea.get("protein_g") or 0),
                    carbohydrates_g=float(
                        idea.get("carbohydrates_g") or 0
                    ),
                    fat_g=float(idea.get("fat_g") or 0),
                    fiber_g=float(idea.get("fiber_g") or 0),
                    sugar_g=float(idea.get("sugar_g") or 0),
                    sodium_mg=float(idea.get("sodium_mg") or 0),
                    food_type="meal",
                )
                entry = add_food_entry(
                    entry_date=today,
                    meal_category=meal_type,
                    food_id=int(created["food"]["food_id"]),
                    quantity=servings,
                    logging_source="telegram_ai",
                    original_text=(
                        "Pantry meal idea: "
                        + str(idea.get("name") or "Pantry meal")
                    ),
                    quantity_is_estimated=True,
                    user_confirmed=True,
                )
            except Exception:
                logging.exception("Pantry meal logging failed")
                send_telegram_msg(
                    "I couldn't log that Pantry meal safely. "
                    "Nothing was added to today's food log.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(today)
            except Exception:
                logging.exception(
                    "Pantry meal Google Sheet sync failed"
                )
                sync_note = (
                    "\n\nThe meal was logged, but the Google Sheet "
                    "totals could not be updated."
                )
            else:
                sync_note = ""

            update_conversation(
                chat_id=chat_id,
                current_step="pantry",
                known_data={
                    "pantry_meal_ideas": [],
                    "pantry_meal_selected_index": None,
                    "pantry_meal_servings": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                append_goal_calorie_progress(
                    "Estimated Pantry meal logged.\n\n"
                    f"Food: {idea.get('name') or 'Pantry meal'}\n"
                    f"Meal: {meal_type.title()}\n"
                    "Calories: "
                    f"{format_display_number(float(entry.get('calories') or 0), decimals=0)}\n"
                    "Protein: "
                    f"{format_display_number(float(entry.get('protein_g') or 0))} g\n\n"
                    "This food-log entry is marked as estimated."
                    + sync_note,
                    today,
                )
                + "\n\n"
                + healthcoach_pantry_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_view":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            page = int(known_data.get("pantry_page") or 0)
            if lowered in {"next", "next page"}:
                show_pantry_view_page(chat_id=chat_id, page=page + 1)
                return
            if lowered in {"previous", "previous page", "prev"}:
                show_pantry_view_page(chat_id=chat_id, page=page - 1)
                return

            show_pantry_view_page(chat_id=chat_id, page=page)
            return

        if current_step == "shopping_list":
            if lowered in {"1", "view", "view list", "view shopping list"}:
                items = list_shopping_items()
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list_view",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_shopping_list(items),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "add",
                "add manually",
                "add shopping items",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_add_items",
                    known_data={"shopping_pending_names": []},
                    missing_fields=["shopping_items"],
                )
                send_telegram_msg(
                    "Send Shopping List items separated by commas.\n\n"
                    "Example: low-sodium broth, olive oil, lemons",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "3",
                "mark purchased",
                "purchased",
            }:
                items = list_shopping_items()
                if not items:
                    send_telegram_msg(
                        "Your Shopping List is empty.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_purchase_select",
                    known_data={
                        "shopping_item_ids": [
                            int(item["shopping_list_item_id"])
                            for item in items
                        ]
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_shopping_item_choices(items, action="purchase"),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "remove", "remove item"}:
                items = list_shopping_items()
                if not items:
                    send_telegram_msg(
                        "Your Shopping List is empty.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_remove_select",
                    known_data={
                        "shopping_item_ids": [
                            int(item["shopping_list_item_id"])
                            for item in items
                        ]
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_shopping_item_choices(items, action="remove"),
                    chat_id=chat_id,
                )
                return

            if lowered in {"5", "clear", "clear list"}:
                items = list_shopping_items()
                if not items:
                    send_telegram_msg(
                        "Your Shopping List is already empty.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_clear_confirmation",
                    known_data={"shopping_clear_count": len(items)},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Clear the Shopping List?\n\n"
                    f"This will remove all {len(items)} item(s). "
                    "My Pantry will not change.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            if lowered in {"6", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_shopping_list_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "shopping_list_view":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                format_shopping_list(list_shopping_items()),
                chat_id=chat_id,
            )
            return

        if current_step == "shopping_add_items":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return
            try:
                names = parse_shopping_item_list(text)
            except ValueError as error:
                send_telegram_msg(str(error), chat_id=chat_id)
                return
            if not names:
                send_telegram_msg(
                    "Send at least one item, separated by commas or lines.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="shopping_add_confirmation",
                known_data={"shopping_pending_names": names},
                missing_fields=[],
            )
            send_telegram_msg(
                "Add these items to the Shopping List?\n\n"
                + "\n".join(f"- {name}" for name in names)
                + "\n\n1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "shopping_add_confirmation":
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No Shopping List items were added.\n\n"
                    + healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "add"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            try:
                result = add_shopping_items(
                    list(known_data.get("shopping_pending_names") or [])
                )
            except Exception:
                logging.exception("Could not add Shopping List items")
                send_telegram_msg(
                    "I couldn't add those items. Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            created_count = len(result["created"])
            existing_count = len(result["existing"])
            summary = f"Added {created_count} item(s) to the Shopping List."
            if existing_count:
                summary += f" {existing_count} item(s) were already there."
            update_conversation(
                chat_id=chat_id,
                current_step="shopping_list",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                summary + "\n\n" + healthcoach_shopping_list_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "shopping_purchase_select",
            "shopping_remove_select",
        }:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return
            shopping_ids = list(known_data.get("shopping_item_ids") or [])
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(shopping_ids):
                send_telegram_msg(
                    "Choose one of the numbered Shopping List items, or "
                    "reply Back.",
                    chat_id=chat_id,
                )
                return
            item = get_shopping_item(int(shopping_ids[selection - 1]))
            if item is None:
                send_telegram_msg(
                    "That Shopping List item is no longer available.",
                    chat_id=chat_id,
                )
                return
            purchasing = current_step == "shopping_purchase_select"
            next_step = (
                "shopping_purchase_confirmation"
                if purchasing
                else "shopping_remove_confirmation"
            )
            update_conversation(
                chat_id=chat_id,
                current_step=next_step,
                known_data={
                    "shopping_selected_id": int(
                        item["shopping_list_item_id"]
                    ),
                    "shopping_selected_name": item["display_name"],
                },
                missing_fields=[],
            )
            prompt = (
                "Mark this Shopping List item purchased?\n\n"
                f"{item['display_name']}\n\n"
                "It will move to My Pantry.\n\n1. Yes\n2. No"
                if purchasing
                else "Remove this Shopping List item?\n\n"
                f"{item['display_name']}\n\n1. Yes\n2. No"
            )
            send_telegram_msg(prompt, chat_id=chat_id)
            return

        if current_step in {
            "shopping_purchase_confirmation",
            "shopping_remove_confirmation",
        }:
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Nothing was changed.\n\n"
                    + healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "remove", "purchased"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            item_id = int(known_data.get("shopping_selected_id") or 0)
            name = str(
                known_data.get("shopping_selected_name") or "Shopping item"
            )
            purchasing = current_step == "shopping_purchase_confirmation"
            try:
                if purchasing:
                    mark_shopping_item_purchased(item_id)
                    message = (
                        f"Moved {name} from the Shopping List to My Pantry."
                    )
                else:
                    removed = remove_shopping_item(item_id)
                    message = (
                        f"Removed {name} from the Shopping List."
                        if removed
                        else "That Shopping List item was already removed."
                    )
            except Exception:
                logging.exception("Could not update Shopping List item")
                send_telegram_msg(
                    "I couldn't update that item. Nothing was removed from "
                    "the Shopping List.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="shopping_list",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                message + "\n\n" + healthcoach_shopping_list_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "shopping_clear_confirmation":
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="shopping_list",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "The Shopping List was not cleared.\n\n"
                    + healthcoach_shopping_list_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "clear"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            removed_count = clear_shopping_list()
            update_conversation(
                chat_id=chat_id,
                current_step="shopping_list",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                f"Cleared {removed_count} item(s) from the Shopping List.\n\n"
                + healthcoach_shopping_list_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_add_items":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            try:
                names = parse_pantry_item_list(text)
            except ValueError as error:
                send_telegram_msg(str(error), chat_id=chat_id)
                return

            if not names:
                send_telegram_msg(
                    "Send at least one Pantry item. Separate multiple "
                    "items with commas or put each one on a new line.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="pantry_add_confirmation",
                known_data={"pantry_pending_names": names},
                missing_fields=[],
            )
            item_lines = "\n".join(
                f"- {name}" for name in names
            )
            send_telegram_msg(
                "Add these items to My Pantry?\n\n"
                f"{item_lines}\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_add_confirmation":
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={"pantry_pending_names": []},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No Pantry items were added.\n\n"
                    + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "add"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            names = list(
                known_data.get("pantry_pending_names") or []
            )
            try:
                result = add_pantry_items(names)
            except Exception:
                logging.exception("Could not add Pantry items")
                send_telegram_msg(
                    "I couldn't add those Pantry items. Nothing was changed.",
                    chat_id=chat_id,
                )
                return

            created_count = len(result["created"])
            existing_count = len(result["existing"])
            summary = f"Added {created_count} item(s) to My Pantry."
            if existing_count:
                summary += (
                    f" {existing_count} item(s) were already there."
                )

            update_conversation(
                chat_id=chat_id,
                current_step="pantry",
                known_data={"pantry_pending_names": []},
                missing_fields=[],
            )
            send_telegram_msg(
                summary + "\n\n" + healthcoach_pantry_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_nutrition_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return
            page = int(known_data.get("pantry_nutrition_page") or 0)
            skipped_ids = list(
                known_data.get("pantry_nutrition_skipped_ids") or []
            )
            if lowered in {"next", "next page"}:
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    page=page + 1,
                    skipped_ids=skipped_ids,
                )
                return
            if lowered in {"previous", "previous page", "prev"}:
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    page=page - 1,
                    skipped_ids=skipped_ids,
                )
                return
            pantry_ids = list(
                known_data.get("pantry_nutrition_item_ids") or []
            )
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(pantry_ids):
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    page=page,
                    skipped_ids=skipped_ids,
                )
                return
            show_pantry_nutrition_options(
                chat_id=chat_id,
                pantry_item_id=int(pantry_ids[selection - 1]),
                known_data=known_data,
            )
            return

        if current_step == "pantry_nutrition_options":
            pantry_item_id = int(
                known_data.get("pantry_nutrition_item_id") or 0
            )
            organizer_mode = bool(
                known_data.get("_pantry_organizer_nutrition_return")
            )
            pantry_item = get_pantry_item_by_id(pantry_item_id)
            if pantry_item is None:
                if organizer_mode:
                    show_pantry_organizer_page(
                        chat_id=chat_id,
                        page=int(known_data.get("pantry_page") or 0),
                    )
                    return
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    skipped_ids=list(
                        known_data.get("pantry_nutrition_skipped_ids") or []
                    ),
                )
                return
            if organizer_mode and lowered in {"6", "7", "back"}:
                show_pantry_organize_nutrition(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=int(known_data.get("pantry_page") or 0),
                )
                return
            if not organizer_mode and lowered in {"7", "back"}:
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    page=int(
                        known_data.get("pantry_nutrition_page") or 0
                    ),
                    skipped_ids=list(
                        known_data.get("pantry_nutrition_skipped_ids") or []
                    ),
                )
                return
            if lowered in {
                "1",
                "link existing nutrition",
                "link saved food",
                "saved food",
            }:
                show_pantry_saved_food_page(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            if lowered in {"2", "try verified lookup", "verified lookup"}:
                send_telegram_msg(
                    "I'm checking for one exact verified nutrition match. "
                    "This may take a moment.",
                    chat_id=chat_id,
                )
                try:
                    result = lookup_official_nutrition(
                        restaurant=None,
                        food_name=str(pantry_item["display_name"]),
                        size=None,
                        brand=None,
                    )
                except Exception:
                    logging.exception("Pantry verified nutrition lookup failed")
                    send_telegram_msg(
                        "I couldn't complete the verified lookup right now. "
                        "Nothing was linked or saved.",
                        chat_id=chat_id,
                    )
                    return
                if not result.get("found"):
                    notes = list(result.get("notes") or [])
                    note_text = (
                        "\n".join(f"- {note}" for note in notes)
                        or "- No complete exact verified match was found."
                    )
                    send_telegram_msg(
                        "I couldn't find one complete exact verified match.\n\n"
                        f"{note_text}\n\n"
                        "Nothing was linked or saved.",
                        chat_id=chat_id,
                    )
                    show_pantry_nutrition_options(
                        chat_id=chat_id,
                        pantry_item_id=pantry_item_id,
                        known_data=known_data,
                    )
                    return
                provider_food = dict(result.get("food") or {})
                provider_nutrition = dict(result.get("nutrition") or {})
                verification = dict(result.get("verification") or {})
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_nutrition_verified_confirmation",
                    known_data={
                        **known_data,
                        "pantry_nutrition_provider_result": result,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_nutrition_review(
                        heading=(
                            "Use this verified nutrition for the Pantry item?"
                        ),
                        pantry_name=str(pantry_item["display_name"]),
                        food=provider_food,
                        nutrition=provider_nutrition,
                        source=str(
                            verification.get("source") or "verified source"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return
            if lowered in {"3", "scan barcode", "barcode"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_barcode_photo",
                    known_data={
                        **known_data,
                        "pantry_nutrition_mode": True,
                        "pantry_scan_mode": False,
                    },
                    missing_fields=["barcode_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of the product barcode for "
                    f"{pantry_item['display_name']}.\n\n"
                    "You can also type the complete barcode number.",
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "4",
                "photograph label",
                "photo label",
                "nutrition label",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_nutrition_label_photo",
                    known_data=known_data,
                    missing_fields=["nutrition_label_photo"],
                )
                send_telegram_msg(
                    "Send a clear, straight-on photo of the Nutrition Facts "
                    f"label for {pantry_item['display_name']}.\n\n"
                    "Include the serving size and every nutrient line. "
                    "Nothing will be saved until you confirm it.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"5", "enter manually", "manual"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_add_serving",
                    known_data={
                        **known_data,
                        "_pantry_nutrition_return": True,
                        "_saved_food_name": str(pantry_item["display_name"]),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What is one serving?\n\n"
                    "Examples: 1 cup, 28 g, 4 ounces, or 1 package.",
                    chat_id=chat_id,
                )
                return
            if not organizer_mode and lowered in {"6", "skip"}:
                skipped_ids = list(
                    known_data.get("pantry_nutrition_skipped_ids") or []
                )
                if pantry_item_id not in skipped_ids:
                    skipped_ids.append(pantry_item_id)
                send_telegram_msg(
                    f"Skipped {pantry_item['display_name']} for this session. "
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    page=int(
                        known_data.get("pantry_nutrition_page") or 0
                    ),
                    skipped_ids=skipped_ids,
                )
                return
            show_pantry_nutrition_options(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
                known_data=known_data,
            )
            return

        if current_step == "pantry_nutrition_saved_food_select":
            page = int(
                known_data.get("pantry_nutrition_saved_food_page") or 0
            )
            if lowered == "back":
                show_pantry_nutrition_options(
                    chat_id=chat_id,
                    pantry_item_id=int(
                        known_data.get("pantry_nutrition_item_id") or 0
                    ),
                    known_data=known_data,
                )
                return
            if lowered in {"next", "next page"}:
                show_pantry_saved_food_page(
                    chat_id=chat_id,
                    known_data=known_data,
                    page=page + 1,
                )
                return
            if lowered in {"previous", "previous page", "prev"}:
                show_pantry_saved_food_page(
                    chat_id=chat_id,
                    known_data=known_data,
                    page=page - 1,
                )
                return
            food_ids = list(
                known_data.get("pantry_nutrition_saved_food_ids") or []
            )
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(food_ids):
                show_pantry_saved_food_page(
                    chat_id=chat_id,
                    known_data=known_data,
                    page=page,
                )
                return
            food_id = int(food_ids[selection - 1])
            selected = next(
                (
                    food
                    for food in pantry_linkable_foods(
                        str(
                            known_data.get("pantry_nutrition_item_name")
                            or "Pantry item"
                        )
                    )
                    if int(food["food_id"]) == food_id
                ),
                None,
            )
            if selected is None or selected.get("calories") is None:
                send_telegram_msg(
                    "That nutrition record is no longer available to link.",
                    chat_id=chat_id,
                )
                show_pantry_saved_food_page(
                    chat_id=chat_id,
                    known_data=known_data,
                    page=page,
                )
                return
            pantry_item = get_pantry_item_by_id(
                int(known_data.get("pantry_nutrition_item_id") or 0)
            )
            if pantry_item is None:
                show_pantry_nutrition_queue(chat_id=chat_id)
                return
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_nutrition_saved_food_confirmation",
                known_data={
                    **known_data,
                    "pantry_nutrition_food_id": food_id,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_pantry_nutrition_review(
                    heading="Link this nutrition to the Pantry item?",
                    pantry_name=str(pantry_item["display_name"]),
                    food=selected,
                    nutrition=selected,
                    source=str(
                        selected.get("verification_source") or "Saved Food"
                    ),
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_nutrition_saved_food_confirmation":
            pantry_item_id = int(
                known_data.get("pantry_nutrition_item_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                show_pantry_saved_food_page(
                    chat_id=chat_id,
                    known_data=known_data,
                    page=int(
                        known_data.get("pantry_nutrition_saved_food_page")
                        or 0
                    ),
                )
                return
            if lowered not in {"1", "yes", "save", "link"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return
            food_id = int(known_data.get("pantry_nutrition_food_id") or 0)
            selected = next(
                (
                    food
                    for food in pantry_linkable_foods(
                        str(
                            known_data.get("pantry_nutrition_item_name")
                            or "Pantry item"
                        )
                    )
                    if int(food["food_id"]) == food_id
                ),
                None,
            )
            try:
                linked = link_pantry_item_to_food(
                    pantry_item_id,
                    food_id=food_id,
                    source="saved_food",
                )
            except (TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't link that nutrition record: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            finish_pantry_nutrition_link(
                chat_id=chat_id,
                known_data=known_data,
                pantry_item=linked,
                food_name=str(
                    (selected or {}).get("canonical_name") or "Saved Food"
                ),
            )
            return

        if current_step == "pantry_nutrition_verified_confirmation":
            pantry_item_id = int(
                known_data.get("pantry_nutrition_item_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                show_pantry_nutrition_options(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    known_data=known_data,
                )
                return
            if lowered not in {"1", "yes", "save", "link"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            result = dict(
                known_data.get("pantry_nutrition_provider_result") or {}
            )
            food = dict(result.get("food") or {})
            nutrition = dict(result.get("nutrition") or {})
            verification = dict(result.get("verification") or {})
            try:
                saved = add_food_with_nutrition(
                    canonical_name=str(food["canonical_name"]),
                    serving_description=str(food["serving_description"]),
                    serving_amount=float(food["serving_amount"]),
                    serving_unit=str(food["serving_unit"]),
                    verification_status=str(verification["status"]),
                    verification_source=str(verification["source"]),
                    calories=float(nutrition["calories"]),
                    protein_g=nutrition.get("protein_g"),
                    carbohydrates_g=nutrition.get("carbohydrates_g"),
                    fat_g=nutrition.get("fat_g"),
                    fiber_g=nutrition.get("fiber_g"),
                    sugar_g=nutrition.get("sugar_g"),
                    sodium_mg=nutrition.get("sodium_mg"),
                    brand=food.get("brand"),
                    restaurant=food.get("restaurant"),
                    food_type=str(food.get("food_type") or "food"),
                    source_item_id=verification.get("source_item_id"),
                    source_url=verification.get("source_url"),
                )
                linked = link_pantry_item_to_food(
                    pantry_item_id,
                    food_id=int(saved["food"]["food_id"]),
                    source="saved_food",
                )
            except (KeyError, TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save and link that verified result: {exc}\n\n"
                    "Nothing was linked.",
                    chat_id=chat_id,
                )
                return
            finish_pantry_nutrition_link(
                chat_id=chat_id,
                known_data=known_data,
                pantry_item=linked,
                food_name=str(saved["food"]["canonical_name"]),
            )
            return

        if current_step == "pantry_nutrition_label_photo":
            if lowered == "back":
                show_pantry_nutrition_options(
                    chat_id=chat_id,
                    pantry_item_id=int(
                        known_data.get("pantry_nutrition_item_id") or 0
                    ),
                    known_data=known_data,
                )
                return
            send_telegram_msg(
                "Send a clear, straight-on Nutrition Facts label photo, "
                "or reply Back.",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_nutrition_label_confirmation":
            pantry_item_id = int(
                known_data.get("pantry_nutrition_item_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_nutrition_label_photo",
                    known_data=known_data,
                    missing_fields=["nutrition_label_photo"],
                )
                send_telegram_msg(
                    "Nothing was saved. Send another clear Nutrition Facts "
                    "label photo, or reply Back.",
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "save", "link"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            label = dict(
                known_data.get("pantry_nutrition_label_result") or {}
            )
            pantry_name = str(
                known_data.get("pantry_nutrition_item_name") or "Pantry item"
            )
            try:
                saved = add_food_with_nutrition(
                    canonical_name=pantry_name,
                    serving_description=str(label["serving_description"]),
                    serving_amount=float(label["serving_amount"]),
                    serving_unit=str(label["serving_unit"]),
                    verification_status="verified",
                    verification_source="user_package_label",
                    calories=float(label["calories"]),
                    protein_g=float(label["protein_g"]),
                    carbohydrates_g=float(label["carbohydrates_g"]),
                    fat_g=float(label["fat_g"]),
                    fiber_g=float(label["fiber_g"]),
                    sugar_g=float(label["sugar_g"]),
                    sodium_mg=float(label["sodium_mg"]),
                    brand=label.get("brand"),
                )
                linked = link_pantry_item_to_food(
                    pantry_item_id,
                    food_id=int(saved["food"]["food_id"]),
                    source="saved_food",
                )
            except (KeyError, TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save and link that label: {exc}\n\n"
                    "Nothing was linked.",
                    chat_id=chat_id,
                )
                return
            finish_pantry_nutrition_link(
                chat_id=chat_id,
                known_data=known_data,
                pantry_item=linked,
                food_name=str(saved["food"]["canonical_name"]),
            )
            return

        if current_step == "pantry_organize_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"search", "find", "find item"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_search_query",
                    known_data={
                        "pantry_page": int(
                            known_data.get("pantry_page") or 0
                        )
                    },
                    missing_fields=["pantry_search_query"],
                )
                send_telegram_msg(
                    "Pantry Organizer — Search\n\n"
                    "Type all or part of the Pantry item name. You can "
                    "also search its linked Food name or brand.\n\n"
                    "Reply Back to return.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            page = int(known_data.get("pantry_page") or 0)
            if lowered in {"next", "next page"}:
                show_pantry_organizer_page(
                    chat_id=chat_id,
                    page=page + 1,
                )
                return
            if lowered in {"previous", "previous page", "prev"}:
                show_pantry_organizer_page(
                    chat_id=chat_id,
                    page=page - 1,
                )
                return

            pantry_ids = list(known_data.get("pantry_item_ids") or [])
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(pantry_ids):
                show_pantry_organizer_page(chat_id=chat_id, page=page)
                return

            pantry_item_id = int(pantry_ids[selection - 1])
            selected = next(
                (
                    item
                    for item in list_pantry_items()
                    if int(item["pantry_item_id"]) == pantry_item_id
                ),
                None,
            )
            if selected is None:
                send_telegram_msg(
                    "That Pantry item is no longer available.",
                    chat_id=chat_id,
                )
                return
            show_pantry_organize_action(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
                page=page,
            )
            return

        if current_step == "pantry_organize_search_query":
            if lowered == "back":
                show_pantry_organizer_page(
                    chat_id=chat_id,
                    page=int(known_data.get("pantry_page") or 0),
                )
                return
            if not text.strip():
                send_telegram_msg(
                    "Type all or part of the Pantry item name, linked "
                    "Food name, or brand. Reply Back to return.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            show_pantry_organizer_search_results(
                chat_id=chat_id,
                query=text,
                page=0,
            )
            return

        if current_step == "pantry_organize_search_results":
            query = str(known_data.get("pantry_search_query") or "")
            search_page = int(known_data.get("pantry_search_page") or 0)
            if lowered == "back":
                show_pantry_organizer_page(chat_id=chat_id, page=0)
                return
            if lowered in {"search", "search again", "new search"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_search_query",
                    known_data={"pantry_page": 0},
                    missing_fields=["pantry_search_query"],
                )
                send_telegram_msg(
                    "Pantry Organizer — Search\n\n"
                    "Type all or part of the Pantry item name. You can "
                    "also search its linked Food name or brand.\n\n"
                    "Reply Back to return.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            if lowered in {"next", "next page"}:
                show_pantry_organizer_search_results(
                    chat_id=chat_id,
                    query=query,
                    page=search_page + 1,
                )
                return
            if lowered in {"previous", "previous page", "prev"}:
                show_pantry_organizer_search_results(
                    chat_id=chat_id,
                    query=query,
                    page=search_page - 1,
                )
                return

            pantry_ids = list(
                known_data.get("pantry_search_item_ids") or []
            )
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(pantry_ids):
                show_pantry_organizer_search_results(
                    chat_id=chat_id,
                    query=query,
                    page=search_page,
                )
                return
            show_pantry_organize_action(
                chat_id=chat_id,
                pantry_item_id=int(pantry_ids[selection - 1]),
                page=0,
                search_query=query,
                search_page=search_page,
            )
            return

        if current_step == "pantry_organize_action":
            page = int(known_data.get("pantry_page") or 0)
            pantry_item_id = int(
                known_data.get("pantry_organize_id") or 0
            )
            if lowered in {"5", "back"}:
                if known_data.get("_pantry_organizer_search_return"):
                    show_pantry_organizer_search_results(
                        chat_id=chat_id,
                        query=str(
                            known_data.get("pantry_search_query") or ""
                        ),
                        page=int(
                            known_data.get("pantry_search_page") or 0
                        ),
                    )
                else:
                    show_pantry_organizer_page(chat_id=chat_id, page=page)
                return
            if lowered in {"1", "change name", "rename"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_rename",
                    known_data=known_data,
                    missing_fields=["display_name"],
                )
                send_telegram_msg(
                    "Current name: "
                    f"{known_data.get('pantry_organize_name')}\n\n"
                    "Type the new Pantry item name.\n"
                    "Reply Back to return without changing it.",
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "2",
                "change storage and food type",
                "storage and food type",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_storage",
                    known_data=known_data,
                    missing_fields=["storage_area"],
                )
                send_telegram_msg(
                    format_pantry_storage_choices(
                        str(
                            known_data.get("pantry_organize_name")
                            or "Pantry item"
                        )
                    ),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "3",
                "view or change nutrition",
                "change nutrition",
                "nutrition",
            }:
                show_pantry_organize_nutrition(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            if lowered in {
                "4",
                "delete pantry item",
                "delete item",
                "delete",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_delete_confirmation",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Delete this Pantry item from My Pantry?\n\n"
                    f"Item: {known_data.get('pantry_organize_name')}\n\n"
                    "Its Saved Food, nutrition, recipes, and previous food "
                    "logs will be preserved. Only its Pantry presence will "
                    "be removed.\n\n"
                    "Nothing has been deleted yet.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return
            show_pantry_organize_action(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
                page=page,
            )
            return

        if current_step == "pantry_organize_nutrition":
            page = int(known_data.get("pantry_page") or 0)
            pantry_item_id = int(
                known_data.get("pantry_organize_id") or 0
            )
            item = get_pantry_item_by_id(pantry_item_id)
            if item is None:
                show_pantry_organizer_page(chat_id=chat_id, page=page)
                return

            if not pantry_nutrition_ready(item):
                if lowered in {"2", "back"}:
                    show_pantry_organize_action(
                        chat_id=chat_id,
                        pantry_item_id=pantry_item_id,
                        page=page,
                    )
                    return
                if lowered in {
                    "1",
                    "add or link nutrition",
                    "add nutrition",
                }:
                    show_pantry_nutrition_options(
                        chat_id=chat_id,
                        pantry_item_id=pantry_item_id,
                        known_data=known_data,
                    )
                    return
                show_pantry_organize_nutrition(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return

            if lowered in {"4", "back"}:
                show_pantry_organize_action(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            if lowered in {
                "1",
                "correct current values",
                "correct nutrition",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_calories",
                    known_data={
                        **known_data,
                        "_saved_food_edit_id": int(item["food_id"]),
                        "_saved_food_edit_name": str(
                            item.get("canonical_name")
                            or item["display_name"]
                        ),
                        "_saved_food_edit_serving": str(
                            item.get("serving_description")
                            or "1 serving"
                        ),
                        "_saved_food_edit_old_version": int(
                            item.get("version_number") or 1
                        ),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Correcting nutrition for "
                    f"{item.get('canonical_name') or item['display_name']}\n"
                    "Serving: "
                    f"{item.get('serving_description') or '1 serving'}\n\n"
                    "This creates a new version for future uses. Previous "
                    "logs and saved recipe calculations will not change.\n\n"
                    "Enter the corrected calories for one serving.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            if lowered in {
                "2",
                "replace linked nutrition",
                "replace nutrition",
            }:
                show_pantry_nutrition_options(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    known_data=known_data,
                )
                return
            if lowered in {
                "3",
                "remove nutrition link",
                "unlink nutrition",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_nutrition_unlink_confirmation",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Remove the nutrition link from this Pantry item?\n\n"
                    f"Item: {item['display_name']}\n"
                    "Linked Food: "
                    f"{item.get('canonical_name') or 'Saved Food'}\n\n"
                    "The Saved Food, nutrition versions, recipes, and "
                    "previous logs will be preserved. This Pantry item "
                    "will return to the needs-nutrition queue.\n\n"
                    "Nothing has been changed yet.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return
            show_pantry_organize_nutrition(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
                page=page,
            )
            return

        if current_step == "pantry_organize_nutrition_unlink_confirmation":
            page = int(known_data.get("pantry_page") or 0)
            pantry_item_id = int(
                known_data.get("pantry_organize_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was changed.", chat_id=chat_id)
                show_pantry_organize_nutrition(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            if lowered not in {"1", "yes", "remove", "unlink"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return
            try:
                item = unlink_pantry_item_nutrition(pantry_item_id)
            except (TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't remove that nutrition link: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                "Nutrition link removed from "
                f"{item['display_name']}.\n\n"
                "The linked Food, nutrition versions, recipes, and previous "
                "logs were preserved. Nothing was logged as eaten.",
                chat_id=chat_id,
            )
            show_pantry_organize_nutrition(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
                page=page,
            )
            return

        if current_step == "pantry_organize_delete_confirmation":
            page = int(known_data.get("pantry_page") or 0)
            pantry_item_id = int(
                known_data.get("pantry_organize_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was deleted.", chat_id=chat_id)
                show_pantry_organize_action(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            if lowered not in {"1", "yes", "delete"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return
            name = str(
                known_data.get("pantry_organize_name") or "Pantry item"
            )
            try:
                removed = remove_pantry_item(pantry_item_id)
            except Exception:
                logging.exception("Pantry Organizer deletion failed")
                send_telegram_msg(
                    "I couldn't delete that Pantry item. Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            message = (
                f"Deleted {name} from My Pantry."
                if removed
                else "That Pantry item was already removed."
            )
            if list_pantry_items():
                send_telegram_msg(
                    message
                    + "\n\nIts Saved Food, nutrition, recipes, and previous "
                    "logs were preserved.",
                    chat_id=chat_id,
                )
                show_pantry_organizer_page(chat_id=chat_id, page=page)
            else:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    message + "\n\n" + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
            return

        if current_step == "pantry_organize_rename":
            page = int(known_data.get("pantry_page") or 0)
            pantry_item_id = int(
                known_data.get("pantry_organize_id") or 0
            )
            if lowered == "back":
                show_pantry_organize_action(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            try:
                revised_name = clean_pantry_name(text)
            except ValueError as exc:
                send_telegram_msg(str(exc), chat_id=chat_id)
                return
            current_name = str(
                known_data.get("pantry_organize_name") or ""
            )
            if revised_name == current_name:
                send_telegram_msg(
                    "That is already the Pantry item's name. "
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                show_pantry_organize_action(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            revised_data = dict(known_data)
            revised_data["pantry_organize_new_name"] = revised_name
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_organize_rename_confirmation",
                known_data=revised_data,
                missing_fields=[],
            )
            send_telegram_msg(
                "Rename this Pantry item?\n\n"
                f"Current name: {current_name}\n"
                f"New name: {revised_name}\n\n"
                "Its nutrition link, source, storage, and food type "
                "will stay the same.\n"
                "Nothing has been changed yet.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_organize_rename_confirmation":
            page = int(known_data.get("pantry_page") or 0)
            pantry_item_id = int(
                known_data.get("pantry_organize_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was changed.", chat_id=chat_id)
                show_pantry_organize_action(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                    page=page,
                )
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return
            try:
                updated = rename_pantry_item(
                    pantry_item_id,
                    display_name=str(
                        known_data.get("pantry_organize_new_name") or ""
                    ),
                )
            except (TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"The Pantry item was not renamed. {exc}",
                    chat_id=chat_id,
                )
                show_pantry_organizer_page(chat_id=chat_id, page=page)
                return
            send_telegram_msg(
                "Pantry item renamed.\n\n"
                f"Old name: {known_data.get('pantry_organize_name')}\n"
                f"New name: {updated['display_name']}\n\n"
                "Its linked data was preserved.",
                chat_id=chat_id,
            )
            show_pantry_organizer_page(chat_id=chat_id, page=page)
            return

        if current_step == "pantry_organize_storage":
            page = int(known_data.get("pantry_page") or 0)
            if lowered == "back":
                show_pantry_organize_action(
                    chat_id=chat_id,
                    pantry_item_id=int(
                        known_data.get("pantry_organize_id") or 0
                    ),
                    page=page,
                )
                return
            storage_area = pantry_choice_key(
                text,
                PANTRY_STORAGE_AREAS,
                aliases={
                    "shelf": "pantry_shelf",
                    "pantry": "pantry_shelf",
                    "fridge": "refrigerator",
                    "cold": "refrigerator",
                    "produce": "counter_produce",
                    "counter": "counter_produce",
                },
            )
            if storage_area is None:
                send_telegram_msg(
                    format_pantry_storage_choices(
                        str(known_data.get("pantry_organize_name") or "Item")
                    ),
                    chat_id=chat_id,
                )
                return
            revised_data = dict(known_data)
            revised_data["pantry_organize_storage"] = storage_area
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_organize_category",
                known_data=revised_data,
                missing_fields=["food_category"],
            )
            send_telegram_msg(
                format_pantry_category_choices(
                    str(known_data.get("pantry_organize_name") or "Item")
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_organize_category":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry_organize_storage",
                    known_data=known_data,
                    missing_fields=["storage_area"],
                )
                send_telegram_msg(
                    format_pantry_storage_choices(
                        str(known_data.get("pantry_organize_name") or "Item")
                    ),
                    chat_id=chat_id,
                )
                return
            food_category = pantry_choice_key(
                text,
                PANTRY_FOOD_CATEGORIES,
                aliases={
                    "meat": "protein",
                    "meat/protein": "protein",
                    "canned": "canned_jarred",
                    "jarred": "canned_jarred",
                    "spices": "baking_spices",
                    "baking": "baking_spices",
                    "beverages": "drinks",
                },
            )
            if food_category is None:
                send_telegram_msg(
                    format_pantry_category_choices(
                        str(known_data.get("pantry_organize_name") or "Item")
                    ),
                    chat_id=chat_id,
                )
                return
            revised_data = dict(known_data)
            revised_data["pantry_organize_category"] = food_category
            update_conversation(
                chat_id=chat_id,
                current_step="pantry_organize_confirmation",
                known_data=revised_data,
                missing_fields=[],
            )
            send_telegram_msg(
                "Save this Pantry organization?\n\n"
                f"Item: {known_data.get('pantry_organize_name')}\n"
                "Storage: "
                f"{PANTRY_STORAGE_AREAS[revised_data['pantry_organize_storage']]}\n"
                "Food type: "
                f"{PANTRY_FOOD_CATEGORIES[food_category]}\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_organize_confirmation":
            page = int(known_data.get("pantry_page") or 0)
            if lowered in {"2", "no", "back"}:
                send_telegram_msg(
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                show_pantry_organizer_page(chat_id=chat_id, page=page)
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return
            try:
                updated = update_pantry_item_organization(
                    int(known_data.get("pantry_organize_id") or 0),
                    storage_area=str(
                        known_data.get("pantry_organize_storage") or ""
                    ),
                    food_category=str(
                        known_data.get("pantry_organize_category") or ""
                    ),
                )
            except (TypeError, ValueError):
                logging.exception("Could not organize Pantry item")
                send_telegram_msg(
                    "That Pantry item could not be updated.",
                    chat_id=chat_id,
                )
                show_pantry_organizer_page(chat_id=chat_id, page=page)
                return
            send_telegram_msg(
                "Pantry organization updated.\n\n"
                f"{updated['display_name']}\n"
                "Storage: "
                f"{PANTRY_STORAGE_AREAS[updated['storage_area']]}\n"
                "Food type: "
                f"{PANTRY_FOOD_CATEGORIES[updated['food_category']]}",
                chat_id=chat_id,
            )
            show_pantry_organizer_page(chat_id=chat_id, page=page)
            return

        if current_step == "pantry_remove_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            page = int(known_data.get("pantry_page") or 0)
            if lowered in {"next", "next page"}:
                show_pantry_remove_page(chat_id=chat_id, page=page + 1)
                return
            if lowered in {"previous", "previous page", "prev"}:
                show_pantry_remove_page(chat_id=chat_id, page=page - 1)
                return

            pantry_ids = list(
                known_data.get("pantry_item_ids") or []
            )
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0

            if selection < 1 or selection > len(pantry_ids):
                show_pantry_remove_page(chat_id=chat_id, page=page)
                return

            pantry_item_id = int(pantry_ids[selection - 1])
            selected = next(
                (
                    item for item in list_pantry_items()
                    if int(item["pantry_item_id"]) == pantry_item_id
                ),
                None,
            )
            if selected is None:
                send_telegram_msg(
                    "That Pantry item is no longer available.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="pantry_remove_confirmation",
                known_data={
                    "pantry_remove_id": pantry_item_id,
                    "pantry_remove_name": selected["display_name"],
                    "pantry_page": page,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Remove this Pantry item?\n\n"
                f"{selected['display_name']}\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_remove_confirmation":
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Nothing was removed.\n\n"
                    + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "remove"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            pantry_item_id = int(
                known_data.get("pantry_remove_id") or 0
            )
            name = str(
                known_data.get("pantry_remove_name")
                or "Pantry item"
            )
            removed = remove_pantry_item(pantry_item_id)
            update_conversation(
                chat_id=chat_id,
                current_step="pantry",
                known_data={},
                missing_fields=[],
            )
            message = (
                f"Removed {name} from My Pantry."
                if removed
                else "That Pantry item was already removed."
            )
            send_telegram_msg(
                message + "\n\n" + healthcoach_pantry_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "pantry_clear_confirmation":
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="pantry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "My Pantry was not cleared.\n\n"
                    + healthcoach_pantry_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "clear"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            removed_count = clear_pantry()
            update_conversation(
                chat_id=chat_id,
                current_step="pantry",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                f"Cleared {removed_count} item(s) from My Pantry.\n\n"
                + healthcoach_pantry_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "photo_tools":
            if lowered in {
                "1",
                "read menu photo",
                "menu photo",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_menu_photo",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Send a clear restaurant menu photo.\n\n"
                    "HealthCoach will recommend up to three "
                    "promising choices using visible information.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "estimate meal photo",
                "meal photo",
                "estimate food photo",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_food_photo",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Send a clear photo of the actual meal.\n\n"
                    "HealthCoach will estimate nutrition, ask for "
                    "important details, and log nothing without "
                    "your confirmation.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "3",
                "scan product barcode",
                "scan barcode",
                "barcode",
                "barcode scanner",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_barcode_photo",
                    known_data={"pantry_scan_mode": False},
                    missing_fields=["barcode_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of a product barcode.\n\n"
                    "Keep the entire barcode and the small digits "
                    "on both ends visible. You can also type the "
                    "barcode number instead.",
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_photo_tools_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "photo_intent":
            intent = None
            if lowered in {
                "1",
                "estimate or log this meal",
                "estimate meal",
                "log meal",
            }:
                intent = "meal"
            elif lowered in {
                "2",
                "read restaurant menu",
                "read a restaurant menu",
                "read menu",
            }:
                intent = "menu"
            elif lowered in {
                "3",
                "scan product barcode",
                "scan barcode",
            }:
                intent = "barcode"
            elif lowered in {
                "4",
                "add scanned product to pantry",
                "add scanned product to my pantry",
                "scan into pantry",
            }:
                intent = "pantry_barcode"
            elif lowered in {
                "5",
                "add visible items to pantry",
                "add visible items to my pantry",
                "add pantry shelf",
                "pantry shelf photo",
            }:
                intent = "pantry_photo"
            elif lowered in {
                "6",
                "import a recipe",
                "import recipe",
                "read recipe",
            }:
                intent = "recipe"
            elif lowered in {"7", "cancel"}:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Photo cancelled. Nothing was saved or logged.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if intent is None:
                send_telegram_msg(
                    healthcoach_photo_intent_text(),
                    chat_id=chat_id,
                )
                return

            file_id = str(
                known_data.get("photo_file_id") or ""
            ).strip()
            caption = str(
                known_data.get("photo_caption") or ""
            ).strip()

            if not file_id:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "That photo expired. Please send it again.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            destination_step = {
                "meal": "await_food_photo",
                "menu": "await_menu_photo",
                "barcode": "await_barcode_photo",
                "pantry_barcode": "await_barcode_photo",
                "pantry_photo": "await_pantry_photo",
                "recipe": "await_recipe_photo",
            }[intent]
            update_conversation(
                chat_id=chat_id,
                current_step=destination_step,
                known_data={
                    "pantry_scan_mode": (
                        intent == "pantry_barcode"
                    ),
                    "pantry_photo_names": (
                        [] if intent == "pantry_photo" else None
                    ),
                },
                missing_fields=[],
            )

            process_telegram_update({
                "message": {
                    "chat": {"id": chat_id},
                    "photo": [{"file_id": file_id}],
                    "caption": caption,
                },
            })
            return

        if current_step == "barcode_teach_offer":
            if lowered == "4":
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Barcode setup cancelled. Nothing was saved.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "1",
                "teach",
                "teach from label",
                "teach this barcode",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_teach_label_photo",
                    known_data=known_data,
                    missing_fields=["nutrition_label_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of the Nutrition Facts label.\n\n"
                    "Include the serving size, calories, protein, "
                    "carbohydrates, fat, fiber, sugar, and sodium. "
                    "Nothing will be saved until you confirm it.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "try another",
                "try another barcode",
                "scan another",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_barcode_photo",
                    known_data={},
                    missing_fields=["barcode_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of a product barcode, or "
                    "type the complete number beneath the bars.",
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "back"}:
                if known_data.get("pantry_nutrition_mode"):
                    show_pantry_nutrition_options(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            known_data.get("pantry_nutrition_item_id") or 0
                        ),
                        known_data=known_data,
                    )
                    return
                if known_data.get("pantry_scan_mode"):
                    destination_step = "pantry"
                    destination_message = healthcoach_pantry_menu_text()
                else:
                    destination_step = "photo_tools"
                    destination_message = (
                        healthcoach_photo_tools_menu_text()
                    )
                update_conversation(
                    chat_id=chat_id,
                    current_step=destination_step,
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    destination_message,
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                "Teach this barcode from the package label?\n\n"
                "1. Teach from label\n"
                "2. Try another barcode\n"
                "3. Back\n"
                "4. Cancel",
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_teach_label_photo":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_teach_offer",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Teach this barcode from the package label?\n\n"
                    "1. Teach from label\n"
                    "2. Try another barcode\n"
                    "3. Back\n"
                    "4. Cancel",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                "Send a clear photo of the Nutrition Facts label.\n\n"
                "The serving size and every nutrient line must be visible.",
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_teach_product_name":
            product_name = text.strip()

            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_teach_label_photo",
                    known_data=known_data,
                    missing_fields=["nutrition_label_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of the Nutrition Facts label.",
                    chat_id=chat_id,
                )
                return

            if not product_name or len(product_name) > 120:
                send_telegram_msg(
                    "Enter a product name between 1 and 120 characters.",
                    chat_id=chat_id,
                )
                return

            label = dict(known_data.get("barcode_label") or {})
            suggested_brand = str(
                label.get("brand") or ""
            ).strip()
            brand_hint = (
                f"\n\nThe photo may show: {suggested_brand}"
                if suggested_brand
                else ""
            )
            update_conversation(
                chat_id=chat_id,
                current_step="barcode_teach_brand",
                known_data={
                    **known_data,
                    "barcode_product_name": product_name,
                },
                missing_fields=["brand"],
            )
            send_telegram_msg(
                "What brand is this product?"
                f"{brand_hint}\n\n"
                "Type the brand, or reply Skip if there is none.",
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_teach_brand":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_teach_product_name",
                    known_data=known_data,
                    missing_fields=["product_name"],
                )
                send_telegram_msg(
                    "What should this product be called in HealthCoach?",
                    chat_id=chat_id,
                )
                return

            brand = None if lowered in {"skip", "none", "no brand"} else text.strip()
            if brand is not None and len(brand) > 120:
                send_telegram_msg(
                    "Enter a brand under 120 characters, or reply Skip.",
                    chat_id=chat_id,
                )
                return

            barcode = str(known_data.get("barcode") or "")
            product_name = str(
                known_data.get("barcode_product_name") or ""
            )
            label = dict(known_data.get("barcode_label") or {})

            if not barcode or not product_name or not label:
                send_telegram_msg(
                    "The barcode setup is incomplete. Please start the "
                    "scan again.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="barcode_teach_confirmation",
                known_data={
                    **known_data,
                    "barcode_brand": brand,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_barcode_teaching_confirmation(
                    barcode=barcode,
                    product_name=product_name,
                    brand=brand,
                    label=label,
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_teach_confirmation":
            if lowered == "3":
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Barcode setup cancelled. Nothing was saved.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "1",
                "yes",
                "yes save it",
                "yes, save it",
                "save",
            }:
                barcode = str(known_data.get("barcode") or "")
                product_name = str(
                    known_data.get("barcode_product_name") or ""
                )
                brand = known_data.get("barcode_brand")
                label = dict(
                    known_data.get("barcode_label") or {}
                )
                result = build_taught_barcode_result(
                    barcode=barcode,
                    product_name=product_name,
                    brand=brand,
                    label=label,
                )

                try:
                    saved = save_barcode_product_result(
                        result,
                        barcode=barcode,
                    )
                except Exception:
                    logging.exception(
                        "Taught barcode product save failed"
                    )
                    send_telegram_msg(
                        "I couldn't save that label and barcode. "
                        "Nothing was changed.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_result",
                    known_data={
                        **known_data,
                        "barcode_result": result,
                        "barcode_saved": True,
                        "barcode_food_id": int(
                            saved["food"]["food_id"]
                        ),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Saved this product and taught HealthCoach "
                    "the barcode. It will be recognized locally "
                    "next time.\n\n"
                    + format_barcode_product(
                        result,
                        barcode=barcode,
                        saved=True,
                        pantry_item_name=known_data.get(
                            "pantry_nutrition_item_name"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "retake",
                "retake label photo",
                "change",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_teach_label_photo",
                    known_data=known_data,
                    missing_fields=["nutrition_label_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of the Nutrition Facts label.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_barcode_teaching_confirmation(
                    barcode=str(known_data.get("barcode") or ""),
                    product_name=str(
                        known_data.get("barcode_product_name") or ""
                    ),
                    brand=known_data.get("barcode_brand"),
                    label=dict(
                        known_data.get("barcode_label") or {}
                    ),
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_result":
            result = dict(
                known_data.get("barcode_result") or {}
            )
            barcode = str(
                known_data.get("barcode") or ""
            )
            barcode_saved = bool(
                known_data.get("barcode_saved")
            )

            if known_data.get("pantry_nutrition_mode") and lowered in {
                "link nutrition",
                "link",
                "use for pantry item",
            }:
                pantry_item_id = int(
                    known_data.get("pantry_nutrition_item_id") or 0
                )
                try:
                    saved = save_barcode_product_result(
                        result,
                        barcode=barcode,
                    )
                    linked = link_pantry_item_to_food(
                        pantry_item_id,
                        food_id=int(saved["food"]["food_id"]),
                        source="barcode",
                        barcode_text=barcode,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    send_telegram_msg(
                        f"I couldn't link that barcode nutrition: {exc}\n\n"
                        "Nothing was linked.",
                        chat_id=chat_id,
                    )
                    return
                finish_pantry_nutrition_link(
                    chat_id=chat_id,
                    known_data=known_data,
                    pantry_item=linked,
                    food_name=str(saved["food"]["canonical_name"]),
                )
                return

            if lowered in {
                "add to pantry",
                "add pantry",
                "pantry",
            }:
                try:
                    saved = save_barcode_product_result(
                        result,
                        barcode=barcode,
                    )
                    product = dict(result.get("food") or {})
                    pantry_item = add_pantry_item(
                        display_name=str(
                            product.get("canonical_name")
                            or "Scanned product"
                        ),
                        food_id=int(saved["food"]["food_id"]),
                        source="barcode",
                        barcode_text=barcode,
                    )
                except Exception:
                    logging.exception(
                        "Could not add barcode product to Pantry"
                    )
                    send_telegram_msg(
                        "I couldn't add that scanned product to "
                        "My Pantry. Nothing was changed.",
                        chat_id=chat_id,
                    )
                    return

                pantry_message = (
                    "Added this scanned product to My Pantry."
                    if pantry_item.get("created")
                    else "This product is already in My Pantry."
                )
                if known_data.get("pantry_scan_mode"):
                    update_conversation(
                        chat_id=chat_id,
                        current_step="pantry",
                        known_data={
                            **known_data,
                            "barcode_saved": True,
                            "barcode_food_id": int(
                                saved["food"]["food_id"]
                            ),
                            "pantry_scan_mode": False,
                        },
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        pantry_message
                        + "\n\n"
                        + healthcoach_pantry_menu_text(),
                        chat_id=chat_id,
                    )
                else:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="barcode_result",
                        known_data={
                            **known_data,
                            "barcode_saved": True,
                            "barcode_food_id": int(
                                saved["food"]["food_id"]
                            ),
                        },
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        pantry_message
                        + "\n\n"
                        + format_barcode_product(
                            result,
                            barcode=barcode,
                            saved=True,
                            pantry_item_name=known_data.get(
                                "pantry_nutrition_item_name"
                            ),
                        ),
                        chat_id=chat_id,
                    )
                return

            if lowered in {
                "1",
                "save product",
                "save",
            }:
                try:
                    saved = save_barcode_product_result(
                        result,
                        barcode=barcode,
                    )
                except Exception:
                    logging.exception(
                        "Barcode product save failed"
                    )
                    send_telegram_msg(
                        "I couldn't save that barcode product. "
                        "Nothing was changed.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_result",
                    known_data={
                        **known_data,
                        "barcode_saved": True,
                        "barcode_food_id": int(
                            saved["food"]["food_id"]
                        ),
                    },
                    missing_fields=[],
                )

                send_telegram_msg(
                    format_barcode_product(
                        result,
                        barcode=barcode,
                        saved=True,
                        pantry_item_name=known_data.get(
                            "pantry_nutrition_item_name"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "log it",
                "log product",
                "log",
            }:
                try:
                    saved = save_barcode_product_result(
                        result,
                        barcode=barcode,
                    )
                except Exception:
                    logging.exception(
                        "Barcode product preparation failed"
                    )
                    send_telegram_msg(
                        "I couldn't prepare that product for "
                        "logging. Nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_log_quantity",
                    known_data={
                        **known_data,
                        "barcode_saved": True,
                        "barcode_food_id": int(
                            saved["food"]["food_id"]
                        ),
                    },
                    missing_fields=["quantity"],
                )

                send_telegram_msg(
                    "How many servings should be logged?\n\n"
                    "Examples: 0.5, 1, or 2.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "3",
                "scan another",
                "scan another barcode",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_barcode_photo",
                    known_data={},
                    missing_fields=["barcode_photo"],
                )
                send_telegram_msg(
                    "Send a clear photo of a product barcode.",
                    chat_id=chat_id,
                )
                return

            if lowered == "back":
                if known_data.get("pantry_nutrition_mode"):
                    show_pantry_nutrition_options(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            known_data.get("pantry_nutrition_item_id") or 0
                        ),
                        known_data=known_data,
                    )
                    return
                if known_data.get("pantry_scan_mode"):
                    destination_step = "pantry"
                    destination_message = healthcoach_pantry_menu_text()
                else:
                    destination_step = "photo_tools"
                    destination_message = (
                        healthcoach_photo_tools_menu_text()
                    )
                update_conversation(
                    chat_id=chat_id,
                    current_step=destination_step,
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    destination_message,
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_barcode_product(
                    result,
                    barcode=barcode,
                    saved=barcode_saved,
                    pantry_item_name=known_data.get(
                        "pantry_nutrition_item_name"
                    ),
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_log_quantity":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_result",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_barcode_product(
                        dict(
                            known_data.get(
                                "barcode_result"
                            ) or {}
                        ),
                        barcode=str(
                            known_data.get("barcode")
                            or ""
                        ),
                        saved=True,
                        pantry_item_name=known_data.get(
                            "pantry_nutrition_item_name"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return

            try:
                quantity = float(text.strip())
            except ValueError:
                quantity = 0.0

            if quantity <= 0 or quantity > 100:
                send_telegram_msg(
                    "Enter a positive number of servings, "
                    "such as 0.5, 1, or 2.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="barcode_log_meal",
                known_data={
                    **known_data,
                    "barcode_quantity": quantity,
                },
                missing_fields=["meal_category"],
            )
            send_telegram_msg(
                "Which meal should this barcode product "
                "be logged under?\n\n"
                "1. Before breakfast\n"
                "2. Breakfast\n"
                "3. Morning snack\n"
                "4. Lunch\n"
                "5. Afternoon snack\n"
                "6. Dinner\n"
                "7. Dessert",
                chat_id=chat_id,
            )
            return

        if current_step == "barcode_log_meal":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_log_quantity",
                    known_data=known_data,
                    missing_fields=["quantity"],
                )
                send_telegram_msg(
                    "How many servings should be logged?\n\n"
                    "Examples: 0.5, 1, or 2.",
                    chat_id=chat_id,
                )
                return

            meal_choices = {
                "1": "before breakfast",
                "before breakfast": "before breakfast",
                "2": "breakfast",
                "breakfast": "breakfast",
                "3": "morning snack",
                "morning snack": "morning snack",
                "school snack": "morning snack",
                "4": "lunch",
                "lunch": "lunch",
                "5": "afternoon snack",
                "afternoon snack": "afternoon snack",
                "6": "dinner",
                "dinner": "dinner",
                "7": "dessert",
                "dessert": "dessert",
            }

            meal_category = meal_choices.get(lowered)
            if meal_category is None:
                send_telegram_msg(
                    "Choose one of the listed meals.",
                    chat_id=chat_id,
                )
                return

            result = dict(
                known_data.get("barcode_result") or {}
            )
            product = dict(result.get("food") or {})
            quantity = float(
                known_data.get("barcode_quantity")
                or 1.0
            )
            food_id = int(
                known_data.get("barcode_food_id")
            )
            today = datetime.now(PACIFIC_TZ).date()

            try:
                entry = add_food_entry(
                    entry_date=today,
                    meal_category=meal_category,
                    food_id=food_id,
                    quantity=quantity,
                    logging_source="barcode",
                    original_text=(
                        "Barcode "
                        + str(
                            known_data.get("barcode")
                            or ""
                        )
                    ),
                    quantity_is_estimated=False,
                    user_confirmed=True,
                )
            except Exception:
                logging.exception(
                    "Barcode food logging failed"
                )
                send_telegram_msg(
                    "I couldn't log that scanned product. "
                    "Nothing was added to today's food log.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(
                    today
                )
            except Exception:
                logging.exception(
                    "Barcode Google Sheet sync failed"
                )
                sync_note = (
                    "\n\nThe food was logged, but the Google "
                    "Sheet totals could not be updated."
                )
            else:
                sync_note = ""

            update_conversation(
                chat_id=chat_id,
                current_step="photo_tools",
                known_data={},
                missing_fields=[],
            )

            send_telegram_msg(
                append_goal_calorie_progress(
                    "Barcode product logged.\n\n"
                    f"Food: "
                    f"{product.get('canonical_name') or 'Scanned product'}\n"
                    f"Meal: {meal_category.title()}\n"
                    f"Quantity: "
                    f"{format_display_number(quantity)} serving(s)\n"
                    f"Calories: "
                    f"{format_display_number(float(entry.get('calories') or 0), decimals=0)}\n"
                    f"Protein: "
                    f"{format_display_number(float(entry.get('protein_g') or 0))} g"
                    f"{sync_note}",
                    today,
                ),
                chat_id=chat_id,
            )
            send_telegram_msg(
                healthcoach_photo_tools_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "await_menu_photo",
            "await_food_photo",
            "await_barcode_photo",
            "await_barcode_number",
        }:
            if lowered == "back":
                if known_data.get("pantry_nutrition_mode"):
                    show_pantry_nutrition_options(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            known_data.get("pantry_nutrition_item_id") or 0
                        ),
                        known_data=known_data,
                    )
                    return
                if known_data.get("pantry_scan_mode"):
                    destination_step = "pantry"
                    destination_message = healthcoach_pantry_menu_text()
                else:
                    destination_step = "photo_tools"
                    destination_message = (
                        healthcoach_photo_tools_menu_text()
                    )
                update_conversation(
                    chat_id=chat_id,
                    current_step=destination_step,
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    destination_message,
                    chat_id=chat_id,
                )
                return

            if current_step in {
                "await_barcode_photo",
                "await_barcode_number",
            }:
                try:
                    barcode = normalize_barcode(text)
                except ValueError:
                    send_telegram_msg(
                        "That isn't a valid barcode number.\n\n"
                        "Type all 8, 12, 13, or 14 digits printed "
                        "beneath the bars, including small digits "
                        "on either end.",
                        chat_id=chat_id,
                    )
                    return

                send_telegram_msg(
                    "I'm checking that exact barcode in USDA.",
                    chat_id=chat_id,
                )

                try:
                    result = lookup_barcode_nutrition(
                        barcode
                    )
                except Exception:
                    logging.exception(
                        "Typed barcode lookup failed"
                    )
                    send_telegram_msg(
                        "I couldn't complete the barcode lookup "
                        "right now. Please try again.",
                        chat_id=chat_id,
                    )
                    return

                if not result.get("found"):
                    notes = list(result.get("notes") or [])
                    note_text = (
                        "\n".join(f"- {note}" for note in notes)
                        or "- No exact usable USDA product was found."
                    )
                    update_conversation(
                        chat_id=chat_id,
                        current_step="barcode_teach_offer",
                        known_data={
                            "barcode": barcode,
                            "barcode_lookup_notes": notes,
                        },
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        "I couldn't retrieve complete verified "
                        f"nutrition for {barcode}.\n\n"
                        f"{note_text}\n\n"
                        "Teach this barcode from the package label?\n\n"
                        "1. Teach from label\n"
                        "2. Try another barcode\n"
                        "3. Back\n"
                        "4. Cancel",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="barcode_result",
                    known_data={
                        "barcode": barcode,
                        "barcode_result": result,
                        "barcode_saved": bool(
                            result.get("saved_food_id")
                        ),
                        "barcode_food_id": result.get(
                            "saved_food_id"
                        ),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_barcode_product(
                        result,
                        barcode=barcode,
                        saved=bool(
                            result.get("saved_food_id")
                        ),
                        pantry_item_name=known_data.get(
                            "pantry_nutrition_item_name"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return

            if current_step == "await_menu_photo":
                message = "Send a clear restaurant menu photo."
            elif current_step == "await_food_photo":
                message = (
                    "Send a clear photo of the actual meal."
                )
            elif current_step == "await_barcode_photo":
                message = (
                    "Send a clear photo of a product barcode, or "
                    "type the complete number beneath the bars."
                )
            else:
                message = (
                    "Type the barcode number printed beneath the bars."
                )

            send_telegram_msg(
                message + "\n\nReply Back or Cancel to leave.",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipes":
            if lowered in {"1", "browse", "browse saved recipes"}:
                recipes = list_saved_recipes()
                if not recipes:
                    send_telegram_msg(
                        "There are no Saved Recipes yet. Choose Create "
                        "recipe, or save one from a Pantry meal idea.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_browse",
                    known_data={
                        "saved_recipe_ids": [
                            int(recipe["saved_recipe_id"])
                            for recipe in recipes
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_choices(recipes),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "create",
                "create recipe",
                "add recipe",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_name",
                    known_data={
                        "_recipe_builder_name": None,
                        "_recipe_builder_existing_id": None,
                        "_recipe_builder_meal_type": None,
                        "_recipe_builder_yield": None,
                        "_recipe_builder_ingredients": [],
                        "_recipe_builder_summary": "",
                        "_recipe_builder_steps": [],
                        "_recipe_builder_selected_food_id": None,
                        "_recipe_builder_food_ids": [],
                    },
                    missing_fields=["recipe_name"],
                )
                send_telegram_msg(
                    "Recipe Builder\n\nWhat should this recipe be called?",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "3",
                "import",
                "import recipe",
                "import a recipe",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_input",
                    known_data={},
                    missing_fields=["recipe_text_or_photo"],
                )
                send_telegram_msg(
                    "Recipe Import\n\nPaste the complete recipe text, or "
                    "send a clear photo showing the recipe name, full "
                    "ingredient list, yield, and preparation.\n\n"
                    "Nutrition will be calculated only from matched Saved "
                    "Foods, and nothing will be saved or logged without "
                    "your confirmation.",
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "edit", "edit saved recipe"}:
                recipes = list_saved_recipes()
                if not recipes:
                    send_telegram_msg(
                        "There are no Saved Recipes to edit yet.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_select",
                    known_data={
                        "saved_recipe_ids": [
                            int(recipe["saved_recipe_id"])
                            for recipe in recipes
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_management_choices(
                        recipes,
                        action="edit",
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"5", "delete", "delete saved recipe"}:
                recipes = list_saved_recipes()
                if not recipes:
                    send_telegram_msg(
                        "There are no Saved Recipes to delete.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_delete_select",
                    known_data={
                        "saved_recipe_ids": [
                            int(recipe["saved_recipe_id"])
                            for recipe in recipes
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_management_choices(
                        recipes,
                        action="delete",
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"6", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_saved_recipes_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_import_input":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                "I'm reading the recipe and organizing its ingredients. "
                "This may take a moment.",
                chat_id=chat_id,
            )
            try:
                draft = parse_recipe_text(text)
            except Exception:
                logging.exception("Recipe text import failed")
                send_telegram_msg(
                    "I couldn't extract that recipe reliably. Paste the "
                    "complete recipe again, or send a clear recipe photo. "
                    "Nothing was saved.",
                    chat_id=chat_id,
                )
                return
            start_recipe_import_draft(chat_id=chat_id, draft=draft)
            return

        if current_step == "recipe_import_name":
            name = text.strip()
            if len(name) < 2:
                send_telegram_msg(
                    "Enter a recipe name with at least two characters.",
                    chat_id=chat_id,
                )
                return
            updated = dict(known_data)
            updated["_recipe_builder_name"] = name
            advance_recipe_import_metadata(chat_id=chat_id, known_data=updated)
            return

        if current_step == "recipe_import_meal_type":
            meal_type = {
                "1": "lunch",
                "lunch": "lunch",
                "2": "dinner",
                "dinner": "dinner",
            }.get(lowered)
            if meal_type is None:
                send_telegram_msg(
                    "Choose Lunch or Dinner.",
                    chat_id=chat_id,
                )
                return
            updated = dict(known_data)
            updated["_recipe_builder_meal_type"] = meal_type
            advance_recipe_import_metadata(chat_id=chat_id, known_data=updated)
            return

        if current_step == "recipe_import_yield":
            try:
                yield_servings = float(text.strip())
            except (TypeError, ValueError):
                yield_servings = 0
            if yield_servings <= 0 or yield_servings > 100:
                send_telegram_msg(
                    "Enter a number greater than 0 and no more than 100.",
                    chat_id=chat_id,
                )
                return
            updated = dict(known_data)
            updated["_recipe_builder_yield"] = yield_servings
            advance_recipe_import_metadata(chat_id=chat_id, known_data=updated)
            return

        if current_step == "recipe_import_preparation":
            try:
                steps = parse_saved_recipe_steps(text)
            except ValueError as exc:
                send_telegram_msg(str(exc), chat_id=chat_id)
                return
            updated = dict(known_data)
            updated["_recipe_builder_steps"] = steps
            advance_recipe_import_metadata(chat_id=chat_id, known_data=updated)
            return

        if current_step == "recipe_import_lookup_description":
            pending = list(known_data.get("_recipe_import_pending") or [])
            if not pending:
                continue_recipe_import_resolution(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_ingredient",
                    known_data=known_data,
                    missing_fields=["recipe_ingredient"],
                )
                send_telegram_msg(
                    format_recipe_import_missing(dict(pending[0])),
                    chat_id=chat_id,
                )
                return
            lookup_name = text.strip()
            if len(lookup_name) < 2:
                send_telegram_msg(
                    "Enter a simpler food description with at least two "
                    "characters, or reply Back.",
                    chat_id=chat_id,
                )
                return
            run_recipe_import_verified_lookup(
                chat_id=chat_id,
                known_data=known_data,
                lookup_name=lookup_name,
                generic_match=True,
            )
            return

        if current_step == "recipe_import_ingredient":
            pending = list(known_data.get("_recipe_import_pending") or [])
            if not pending:
                continue_recipe_import_resolution(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            current = dict(pending[0])

            if lowered in {"exclude", "exclude ingredient", "skip"}:
                if not (current.get("optional") or current.get("trace_only")):
                    send_telegram_msg(
                        "This is a major ingredient, so it cannot be "
                        "excluded from recipe nutrition. Match it to a "
                        "Saved Food or reply Add new saved food.",
                        chat_id=chat_id,
                    )
                    return
                excluded = list(
                    known_data.get("_recipe_import_excluded") or []
                )
                excluded.append(
                    " ".join(
                        part for part in (
                            str(current.get("amount_description") or ""),
                            str(current.get("ingredient_name") or ""),
                        ) if part
                    )
                )
                updated = dict(known_data)
                updated["_recipe_import_excluded"] = excluded
                updated["_recipe_import_pending"] = pending[1:]
                continue_recipe_import_resolution(
                    chat_id=chat_id,
                    known_data=updated,
                )
                return

            if lowered in {"include", "include ingredient"}:
                selected_food_id = current.get("candidate_food_id")
                if not selected_food_id:
                    send_telegram_msg(
                        "First type the exact Saved Food name, or reply "
                        "Add new saved food.",
                        chat_id=chat_id,
                    )
                    return
                amount_description = str(
                    current.get("amount_description") or ""
                )
                food = None
            elif lowered in {
                "add new saved food",
                "new saved food",
                "add saved food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_add_name",
                    known_data={
                        **known_data,
                        "_recipe_builder_return": True,
                        "_recipe_import_return": True,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What should this new Saved Food ingredient be called?",
                    chat_id=chat_id,
                )
                return
            elif lowered in {
                "try verified lookup",
                "verified lookup",
                "look up",
                "lookup",
            }:
                run_recipe_import_verified_lookup(
                    chat_id=chat_id,
                    known_data=known_data,
                    lookup_name=str(
                        current.get("ingredient_name") or ""
                    ),
                )
                return
            elif lowered in {
                "try verified generic match",
                "verified generic match",
                "generic match",
            }:
                suggestion = str(
                    current.get("generic_lookup_name") or ""
                ).strip()
                if not suggestion:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="recipe_import_lookup_description",
                        known_data=known_data,
                        missing_fields=["recipe_ingredient_lookup_name"],
                    )
                    send_telegram_msg(
                        "Enter a simpler food description for verified "
                        "lookup.\n\nExample: onion\n\nNothing will be saved "
                        "without your confirmation.",
                        chat_id=chat_id,
                    )
                    return
                run_recipe_import_verified_lookup(
                    chat_id=chat_id,
                    known_data=known_data,
                    lookup_name=suggestion,
                    generic_match=True,
                )
                return
            elif lowered in {
                "enter simpler description",
                "simpler description",
                "different description",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_lookup_description",
                    known_data=known_data,
                    missing_fields=["recipe_ingredient_lookup_name"],
                )
                send_telegram_msg(
                    "Enter a simpler food description for verified lookup."
                    "\n\nExample: onion\n\nNothing will be saved without "
                    "your confirmation.",
                    chat_id=chat_id,
                )
                return
            elif lowered.startswith("amount:"):
                selected_food_id = current.get("candidate_food_id")
                if not selected_food_id:
                    send_telegram_msg(
                        "Choose an existing Saved Food first by typing its "
                        "exact name.",
                        chat_id=chat_id,
                    )
                    return
                amount_description = text.split(":", 1)[1].strip()
                food = None
            elif lowered in {
                "use one saved food serving",
                "use one serving",
                "one serving",
            }:
                selected_food_id = current.get("candidate_food_id")
                if not selected_food_id:
                    send_telegram_msg(
                        "Choose an existing Saved Food first by typing its "
                        "exact name.",
                        chat_id=chat_id,
                    )
                    return
                amount_description = "1 serving"
                food = None
            else:
                food = find_recipe_ingredient_food(text)
                if food is None:
                    send_telegram_msg(
                        format_recipe_import_missing(current),
                        chat_id=chat_id,
                    )
                    return
                updated = dict(known_data)
                updated["_recipe_import_selected_food"] = {
                    "food_id": int(food["food_id"]),
                    "canonical_name": str(
                        food.get("canonical_name") or "Saved Food"
                    ),
                    "serving_description": str(
                        food.get("serving_description") or "not listed"
                    ),
                }
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_saved_food_confirmation",
                    known_data=updated,
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_recipe_import_saved_food_confirmation(
                        ingredient=current,
                        food=food,
                    ),
                    chat_id=chat_id,
                )
                return

            try:
                ingredient = prepare_recipe_ingredient(
                    food_id=int(selected_food_id),
                    amount_description=amount_description,
                )
            except (TypeError, ValueError) as exc:
                current["candidate_food_id"] = int(selected_food_id)
                if food is not None:
                    current["candidate_food_name"] = str(
                        food.get("canonical_name") or ""
                    )
                    current["candidate_serving_description"] = str(
                        food.get("serving_description") or ""
                    )
                current["conversion_error"] = str(exc)
                updated = dict(known_data)
                updated["_recipe_import_pending"] = [current, *pending[1:]]
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_ingredient",
                    known_data=updated,
                    missing_fields=["recipe_ingredient_amount"],
                )
                send_telegram_msg(
                    f"{exc}\n\n" + format_recipe_import_missing(current),
                    chat_id=chat_id,
                )
                return

            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            ingredients.append(
                annotate_imported_recipe_ingredient(ingredient, current)
            )
            updated = dict(known_data)
            updated["_recipe_builder_ingredients"] = ingredients
            updated["_recipe_import_pending"] = pending[1:]
            continue_recipe_import_resolution(
                chat_id=chat_id,
                known_data=updated,
            )
            return

        if current_step == "recipe_import_saved_food_confirmation":
            pending = list(known_data.get("_recipe_import_pending") or [])
            selected = dict(
                known_data.get("_recipe_import_selected_food") or {}
            )
            if not pending or not selected.get("food_id"):
                send_telegram_msg(
                    "That Saved Food selection expired. Please restart "
                    "Recipe Import. Nothing was saved.",
                    chat_id=chat_id,
                )
                return
            updated = dict(known_data)
            updated.pop("_recipe_import_selected_food", None)
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_ingredient",
                    known_data=updated,
                    missing_fields=["recipe_ingredient"],
                )
                send_telegram_msg(
                    format_recipe_import_missing(dict(pending[0])),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes"}:
                send_telegram_msg(
                    "Use this Saved Food for this imported ingredient?\n\n"
                    "1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            current = dict(pending[0])
            try:
                ingredient = prepare_recipe_ingredient(
                    food_id=int(selected["food_id"]),
                    amount_description=str(
                        current.get("amount_description") or ""
                    ),
                )
            except (TypeError, ValueError) as exc:
                current["candidate_food_id"] = int(selected["food_id"])
                current["candidate_food_name"] = str(
                    selected.get("canonical_name") or "Saved Food"
                )
                current["candidate_serving_description"] = str(
                    selected.get("serving_description") or "not listed"
                )
                current["conversion_error"] = str(exc)
                updated["_recipe_import_pending"] = [
                    current,
                    *pending[1:],
                ]
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_ingredient",
                    known_data=updated,
                    missing_fields=["recipe_ingredient_amount"],
                )
                send_telegram_msg(
                    f"{exc}\n\n" + format_recipe_import_missing(current),
                    chat_id=chat_id,
                )
                return

            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            ingredients.append(
                annotate_imported_recipe_ingredient(ingredient, current)
            )
            updated["_recipe_builder_ingredients"] = ingredients
            updated["_recipe_import_pending"] = pending[1:]
            continue_recipe_import_resolution(
                chat_id=chat_id,
                known_data=updated,
            )
            return

        if current_step == "recipe_import_verified_amount":
            pending = list(known_data.get("_recipe_import_pending") or [])
            provider_result = dict(
                known_data.get("_recipe_import_verified_result") or {}
            )
            if not pending or not recipe_import_lookup_is_complete(
                provider_result
            ):
                send_telegram_msg(
                    "That verified result expired. Please restart Recipe "
                    "Import. Nothing was saved.",
                    chat_id=chat_id,
                )
                return
            if lowered == "back":
                updated = dict(known_data)
                updated.pop("_recipe_import_verified_result", None)
                updated.pop("_recipe_import_verified_lookup_name", None)
                updated.pop("_recipe_import_verified_is_generic", None)
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_ingredient",
                    known_data=updated,
                    missing_fields=["recipe_ingredient"],
                )
                send_telegram_msg(
                    format_recipe_import_missing(dict(pending[0])),
                    chat_id=chat_id,
                )
                return
            if lowered in {"use one verified serving", "one serving"}:
                amount_description = "1 serving"
            elif lowered.startswith("amount:"):
                amount_description = text.split(":", 1)[1].strip()
            else:
                send_telegram_msg(
                    "Reply Use one verified serving, or type Amount: "
                    "followed by a compatible amount. Reply Back to choose "
                    "another match.",
                    chat_id=chat_id,
                )
                return
            provider_food = dict(provider_result.get("food") or {})
            try:
                ingredient_serving_multiplier(
                    amount_description=amount_description,
                    serving_amount=float(provider_food["serving_amount"]),
                    serving_unit=str(provider_food["serving_unit"]),
                    serving_description=str(
                        provider_food.get("serving_description") or ""
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"{exc}\n\nReply Use one verified serving, or type "
                    "Amount: followed by another compatible amount.",
                    chat_id=chat_id,
                )
                return
            current = dict(pending[0])
            current["amount_description"] = amount_description
            updated = dict(known_data)
            updated["_recipe_import_pending"] = [current, *pending[1:]]
            show_recipe_import_verified_confirmation(
                chat_id=chat_id,
                known_data=updated,
                provider_result=provider_result,
                lookup_name=str(
                    known_data.get("_recipe_import_verified_lookup_name")
                    or current.get("ingredient_name")
                    or ""
                ),
                generic_match=bool(
                    known_data.get("_recipe_import_verified_is_generic")
                ),
            )
            return

        if current_step == "recipe_import_verified_confirmation":
            if lowered in {"2", "no"}:
                updated = dict(known_data)
                updated.pop("_recipe_import_verified_result", None)
                continue_recipe_import_resolution(
                    chat_id=chat_id,
                    known_data=updated,
                )
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Save this verified ingredient?\n\n1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return
            pending = list(known_data.get("_recipe_import_pending") or [])
            provider_result = dict(
                known_data.get("_recipe_import_verified_result") or {}
            )
            if not pending or not provider_result.get("found"):
                send_telegram_msg(
                    "That verified result expired. Please restart Recipe "
                    "Import. Nothing was saved.",
                    chat_id=chat_id,
                )
                return
            provider_food = dict(provider_result.get("food") or {})
            nutrition = dict(provider_result.get("nutrition") or {})
            verification = dict(provider_result.get("verification") or {})
            try:
                saved = add_food_with_nutrition(
                    canonical_name=str(provider_food["canonical_name"]),
                    serving_description=str(
                        provider_food["serving_description"]
                    ),
                    serving_amount=float(provider_food["serving_amount"]),
                    serving_unit=str(provider_food["serving_unit"]),
                    verification_status="verified",
                    verification_source=str(
                        verification.get("source") or "verified_lookup"
                    ),
                    source_item_id=verification.get("source_item_id"),
                    source_url=verification.get("source_url"),
                    calories=float(nutrition["calories"]),
                    protein_g=float(nutrition["protein_g"]),
                    carbohydrates_g=float(nutrition["carbohydrates_g"]),
                    fat_g=float(nutrition["fat_g"]),
                    fiber_g=float(nutrition["fiber_g"]),
                    sugar_g=float(nutrition["sugar_g"]),
                    sodium_mg=float(nutrition["sodium_mg"]),
                    brand=provider_food.get("brand"),
                    food_type="food",
                )
                food = dict(saved["food"])
                ingredient = prepare_recipe_ingredient(
                    food_id=int(food["food_id"]),
                    amount_description=str(
                        pending[0].get("amount_description") or ""
                    ),
                )
            except Exception:
                logging.exception("Could not save verified recipe ingredient")
                send_telegram_msg(
                    "I couldn't save that verified ingredient safely. "
                    "Nothing was added to the recipe.",
                    chat_id=chat_id,
                )
                return
            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            ingredients.append(
                annotate_imported_recipe_ingredient(ingredient, current)
            )
            created_ids = list(
                known_data.get("_recipe_import_created_food_ids") or []
            )
            if saved.get("created"):
                created_ids.append(int(food["food_id"]))
            updated = dict(known_data)
            updated.pop("_recipe_import_verified_result", None)
            updated["_recipe_builder_ingredients"] = ingredients
            updated["_recipe_import_pending"] = pending[1:]
            updated["_recipe_import_created_food_ids"] = created_ids
            continue_recipe_import_resolution(
                chat_id=chat_id,
                known_data=updated,
            )
            return

        if current_step == "recipe_builder_name":
            name = text.strip()
            if len(name) < 2:
                send_telegram_msg(
                    "Enter a recipe name with at least two characters.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="recipe_builder_meal_type",
                known_data={"_recipe_builder_name": name},
                missing_fields=["meal_type"],
            )
            send_telegram_msg(
                "Is this recipe mainly for lunch or dinner?\n\n"
                "1. Lunch\n"
                "2. Dinner",
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_meal_type":
            meal_type = {
                "1": "lunch",
                "lunch": "lunch",
                "2": "dinner",
                "dinner": "dinner",
            }.get(lowered)
            if meal_type is None:
                send_telegram_msg(
                    "Choose Lunch or Dinner.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="recipe_builder_yield",
                known_data={"_recipe_builder_meal_type": meal_type},
                missing_fields=["yield_servings"],
            )
            send_telegram_msg(
                "How many servings will the entire recipe make?\n\n"
                "Examples: 1, 2, 4, or 6",
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_yield":
            try:
                yield_servings = float(text.strip())
            except (TypeError, ValueError):
                yield_servings = 0
            if yield_servings <= 0 or yield_servings > 100:
                send_telegram_msg(
                    "Enter a number greater than 0 and no more than 100.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                known_data={"_recipe_builder_yield": yield_servings},
                missing_fields=["ingredient"],
            )
            show_recipe_builder_pantry_choices(chat_id=chat_id)
            return

        if current_step == "recipe_builder_edit_select":
            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            if lowered in {"back", "back to review", "review"}:
                updated = dict(known_data)
                updated["_recipe_builder_edit_index"] = None
                updated["_recipe_builder_replace_index"] = None
                updated["_recipe_builder_selected_food_id"] = None
                updated["_recipe_builder_review_editing"] = False
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_confirmation",
                    known_data=updated,
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_recipe_builder_review(updated),
                    chat_id=chat_id,
                )
                return
            if lowered in {"add", "add ingredient", "add another ingredient"}:
                updated = dict(known_data)
                updated["_recipe_builder_review_editing"] = True
                updated["_recipe_builder_replace_index"] = None
                updated["_recipe_builder_selected_food_id"] = None
                update_conversation(
                    chat_id=chat_id,
                    known_data=updated,
                    missing_fields=["ingredient"],
                )
                show_recipe_builder_pantry_choices(chat_id=chat_id)
                return
            try:
                selected_index = int(text.strip()) - 1
            except (TypeError, ValueError):
                selected_index = -1
            if selected_index < 0 or selected_index >= len(ingredients):
                send_telegram_msg(
                    format_recipe_builder_edit_choices(known_data),
                    chat_id=chat_id,
                )
                return
            updated = dict(known_data)
            updated["_recipe_builder_edit_index"] = selected_index
            show_recipe_builder_edit_action(
                chat_id=chat_id,
                known_data=updated,
            )
            return

        if current_step == "recipe_builder_edit_action":
            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            index_value = known_data.get("_recipe_builder_edit_index")
            index = int(index_value) if index_value is not None else -1
            if index < 0 or index >= len(ingredients):
                show_recipe_builder_edit_choices(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            ingredient = dict(ingredients[index])
            if lowered in {"4", "back"}:
                show_recipe_builder_edit_choices(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            if lowered in {"1", "change amount", "amount"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_edit_amount",
                    missing_fields=["ingredient_amount"],
                )
                send_telegram_msg(
                    "Enter the corrected amount used in the entire recipe.\n\n"
                    f"Current: {ingredient.get('amount_description')}\n"
                    f"Saved serving: "
                    f"{ingredient.get('serving_description') or 'not listed'}\n\n"
                    "Examples: 1 serving, 40 g, 3 oz, or 2 slices.\n"
                    "Reply Back to keep the current amount.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "change saved food", "saved food"}:
                updated = dict(known_data)
                updated["_recipe_builder_replace_index"] = index
                updated["_recipe_builder_review_editing"] = True
                update_conversation(
                    chat_id=chat_id,
                    known_data=updated,
                    missing_fields=["ingredient"],
                )
                show_recipe_builder_pantry_choices(chat_id=chat_id)
                return
            if lowered in {"3", "remove", "remove ingredient"}:
                if (
                    known_data.get("_recipe_import_active")
                    and ingredient.get("_recipe_import_source_label")
                    and not ingredient.get("_recipe_import_optional")
                ):
                    send_telegram_msg(
                        "A required imported ingredient cannot be removed. "
                        "Change its amount or Saved Food instead.",
                        chat_id=chat_id,
                    )
                    return
                if len(ingredients) <= 1:
                    send_telegram_msg(
                        "A recipe must keep at least one ingredient.",
                        chat_id=chat_id,
                    )
                    return
                removed = ingredients.pop(index)
                updated = dict(known_data)
                updated["_recipe_builder_ingredients"] = ingredients
                if removed.get("_recipe_import_source_label"):
                    excluded = list(
                        updated.get("_recipe_import_excluded") or []
                    )
                    excluded.append(str(removed["_recipe_import_source_label"]))
                    updated["_recipe_import_excluded"] = excluded
                show_recipe_builder_edit_choices(
                    chat_id=chat_id,
                    known_data=updated,
                )
                return
            send_telegram_msg(
                format_recipe_builder_edit_action(known_data),
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_edit_amount":
            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            index_value = known_data.get("_recipe_builder_edit_index")
            index = int(index_value) if index_value is not None else -1
            if index < 0 or index >= len(ingredients):
                show_recipe_builder_edit_choices(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            if lowered == "back":
                show_recipe_builder_edit_action(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            previous = dict(ingredients[index])
            try:
                corrected = prepare_recipe_ingredient(
                    food_id=int(previous["food_id"]),
                    nutrition_version_id=int(
                        previous["nutrition_version_id"]
                    ),
                    amount_description=text,
                )
            except (KeyError, TypeError, ValueError) as exc:
                send_telegram_msg(str(exc), chat_id=chat_id)
                return
            for field in (
                "_recipe_import_source_label",
                "_recipe_import_optional",
            ):
                if field in previous:
                    corrected[field] = previous[field]
            ingredients[index] = corrected
            updated = dict(known_data)
            updated["_recipe_builder_ingredients"] = ingredients
            show_recipe_builder_edit_choices(
                chat_id=chat_id,
                known_data=updated,
            )
            return

        if current_step in {
            "recipe_builder_ingredient_name",
            "recipe_builder_ingredient_select",
        }:
            if lowered == "back":
                if known_data.get("_recipe_builder_review_editing"):
                    if known_data.get("_recipe_builder_replace_index") is not None:
                        show_recipe_builder_edit_action(
                            chat_id=chat_id,
                            known_data=known_data,
                        )
                    else:
                        show_recipe_builder_edit_choices(
                            chat_id=chat_id,
                            known_data=known_data,
                        )
                    return
                ingredients = list(
                    known_data.get("_recipe_builder_ingredients") or []
                )
                if ingredients:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="recipe_builder_ingredient_menu",
                        known_data={
                            "_recipe_builder_selected_food_id": None,
                        },
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        format_recipe_builder_ingredients(known_data),
                        chat_id=chat_id,
                    )
                else:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="recipe_builder_yield",
                        missing_fields=["yield_servings"],
                    )
                    send_telegram_msg(
                        "How many servings will the entire recipe make?",
                        chat_id=chat_id,
                    )
                return

            if lowered in {"next", "next page"}:
                current_page = int(
                    known_data.get("_recipe_builder_pantry_page") or 1
                )
                total_pages = int(
                    known_data.get("_recipe_builder_pantry_total_pages")
                    or 1
                )
                if current_page >= total_pages:
                    send_telegram_msg(
                        "You are already on the last Pantry page.",
                        chat_id=chat_id,
                    )
                    return
                show_recipe_builder_pantry_choices(
                    chat_id=chat_id,
                    requested_page=current_page + 1,
                )
                return

            if lowered in {"previous", "previous page"}:
                current_page = int(
                    known_data.get("_recipe_builder_pantry_page") or 1
                )
                if current_page <= 1:
                    send_telegram_msg(
                        "You are already on the first Pantry page.",
                        chat_id=chat_id,
                    )
                    return
                show_recipe_builder_pantry_choices(
                    chat_id=chat_id,
                    requested_page=current_page - 1,
                )
                return

            if lowered in {
                "browse",
                "browse foods",
                "browse saved foods",
                "browse pantry",
            }:
                show_recipe_builder_pantry_choices(chat_id=chat_id)
                return

            if lowered in {
                "add new saved food",
                "new saved food",
                "add saved food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_add_name",
                    known_data={"_recipe_builder_return": True},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What should the new Saved Food ingredient be called?",
                    chat_id=chat_id,
                )
                return

            food = None
            if current_step == "recipe_builder_ingredient_select":
                food_ids = list(
                    known_data.get("_recipe_builder_food_ids") or []
                )
                try:
                    selected_index = int(text.strip()) - 1
                    if selected_index < 0:
                        raise IndexError
                    selected_id = int(food_ids[selected_index])
                    food = next(
                        (
                            item
                            for item in list_recipe_ingredient_foods()
                            if int(item["food_id"]) == selected_id
                        ),
                        None,
                    )
                except (IndexError, TypeError, ValueError):
                    food = find_recipe_ingredient_food(text)
            else:
                food = find_recipe_ingredient_food(text)

            if food is None:
                send_telegram_msg(
                    "I couldn't find one trusted Saved Food with that "
                    "name. Choose a Pantry number, type another Saved "
                    "Food name, or reply Add new saved food.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="recipe_builder_ingredient_amount",
                known_data={
                    "_recipe_builder_selected_food_id": int(food["food_id"]),
                },
                missing_fields=["ingredient_amount"],
            )
            send_telegram_msg(
                f"How much {food.get('canonical_name')} is in the "
                "entire recipe?\n\n"
                f"Saved serving: {food.get('serving_description')}\n"
                "Examples: 1 serving, 40 g, 3 oz, or 2 slices.",
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_ingredient_amount":
            if lowered == "back":
                if known_data.get("_recipe_builder_review_editing"):
                    if known_data.get("_recipe_builder_replace_index") is not None:
                        show_recipe_builder_edit_action(
                            chat_id=chat_id,
                            known_data=known_data,
                        )
                    else:
                        show_recipe_builder_edit_choices(
                            chat_id=chat_id,
                            known_data=known_data,
                        )
                    return
                update_conversation(
                    chat_id=chat_id,
                    known_data={"_recipe_builder_selected_food_id": None},
                )
                show_recipe_builder_pantry_choices(
                    chat_id=chat_id,
                    requested_page=int(
                        known_data.get("_recipe_builder_pantry_page") or 1
                    ),
                )
                return
            try:
                ingredient = prepare_recipe_ingredient(
                    food_id=int(
                        known_data.get(
                            "_recipe_builder_selected_food_id"
                        )
                    ),
                    amount_description=text,
                )
            except (TypeError, ValueError) as exc:
                send_telegram_msg(str(exc), chat_id=chat_id)
                return

            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            replace_value = known_data.get("_recipe_builder_replace_index")
            replace_index = (
                int(replace_value) if replace_value is not None else None
            )
            if replace_index is not None:
                if replace_index < 0 or replace_index >= len(ingredients):
                    send_telegram_msg(
                        "That ingredient is no longer available. Return to "
                        "the review and choose it again.",
                        chat_id=chat_id,
                    )
                    return
                previous = dict(ingredients[replace_index])
                for field in (
                    "_recipe_import_source_label",
                    "_recipe_import_optional",
                ):
                    if field in previous:
                        ingredient[field] = previous[field]
                ingredients[replace_index] = ingredient
            else:
                ingredients.append(ingredient)
            updated = dict(known_data)
            updated["_recipe_builder_ingredients"] = ingredients
            updated["_recipe_builder_selected_food_id"] = None
            if known_data.get("_recipe_builder_review_editing"):
                show_recipe_builder_edit_choices(
                    chat_id=chat_id,
                    known_data=updated,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="recipe_builder_ingredient_menu",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                format_recipe_builder_ingredients(updated),
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_ingredient_menu":
            ingredients = list(
                known_data.get("_recipe_builder_ingredients") or []
            )
            if lowered in {"1", "add", "add another ingredient"}:
                show_recipe_builder_pantry_choices(chat_id=chat_id)
                return
            if lowered in {"3", "remove", "remove last ingredient"}:
                if not ingredients:
                    send_telegram_msg(
                        "There is no ingredient to remove.",
                        chat_id=chat_id,
                    )
                    return
                ingredients.pop()
                if not ingredients:
                    update_conversation(
                        chat_id=chat_id,
                        known_data={"_recipe_builder_ingredients": []},
                    )
                    show_recipe_builder_pantry_choices(chat_id=chat_id)
                    return
                updated = dict(known_data)
                updated["_recipe_builder_ingredients"] = ingredients
                update_conversation(
                    chat_id=chat_id,
                    known_data=updated,
                )
                send_telegram_msg(
                    format_recipe_builder_ingredients(updated),
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "continue", "done"}:
                if not ingredients:
                    send_telegram_msg(
                        "Add at least one ingredient before continuing.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_summary",
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Enter a short description of the recipe, or reply Skip.",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                format_recipe_builder_ingredients(known_data),
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_summary":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_ingredient_menu",
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_recipe_builder_ingredients(known_data),
                    chat_id=chat_id,
                )
                return
            summary = "" if lowered in {"skip", "none"} else text.strip()
            update_conversation(
                chat_id=chat_id,
                current_step="recipe_builder_preparation",
                known_data={"_recipe_builder_summary": summary},
                missing_fields=["preparation_steps"],
            )
            send_telegram_msg(
                "Enter the preparation steps, one per line.\n\n"
                "Example:\n"
                "1. Brown the turkey.\n"
                "2. Add the vegetables and simmer.",
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_preparation":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_summary",
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Enter a short description, or reply Skip.",
                    chat_id=chat_id,
                )
                return
            try:
                steps = parse_saved_recipe_steps(text)
            except ValueError as exc:
                send_telegram_msg(str(exc), chat_id=chat_id)
                return
            updated = dict(known_data)
            updated["_recipe_builder_steps"] = steps
            update_conversation(
                chat_id=chat_id,
                current_step="recipe_builder_confirmation",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                format_recipe_builder_review(updated),
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_builder_confirmation":
            if lowered in {"2", "back", "change ingredients"}:
                show_recipe_builder_edit_choices(
                    chat_id=chat_id,
                    known_data=known_data,
                )
                return
            if lowered not in {
                "1",
                "save",
                "save recipe",
                "save changes",
            }:
                save_label = (
                    "Save Changes"
                    if known_data.get("_recipe_builder_existing_id")
                    else "Save Recipe"
                )
                send_telegram_msg(
                    f"Reply {save_label}, Change ingredients, or Cancel.",
                    chat_id=chat_id,
                )
                return
            existing_recipe_id = int(
                known_data.get("_recipe_builder_existing_id") or 0
            )
            try:
                if existing_recipe_id:
                    update_result = update_saved_recipe_from_ingredients(
                        existing_recipe_id,
                        yield_servings=float(
                            known_data.get("_recipe_builder_yield") or 0
                        ),
                        ingredients=list(
                            known_data.get(
                                "_recipe_builder_ingredients"
                            ) or []
                        ),
                        preparation_steps=list(
                            known_data.get("_recipe_builder_steps") or []
                        ),
                        summary=str(
                            known_data.get("_recipe_builder_summary") or ""
                        ),
                    )
                    result = {
                        "created": True,
                        "recipe": update_result["recipe"],
                    }
                else:
                    result = create_saved_recipe_from_ingredients(
                        name=str(
                            known_data.get("_recipe_builder_name") or ""
                        ),
                        meal_type=str(
                            known_data.get("_recipe_builder_meal_type") or ""
                        ),
                        yield_servings=float(
                            known_data.get("_recipe_builder_yield") or 0
                        ),
                        ingredients=list(
                            known_data.get(
                                "_recipe_builder_ingredients"
                            ) or []
                        ),
                        preparation_steps=list(
                            known_data.get("_recipe_builder_steps") or []
                        ),
                        summary=str(
                            known_data.get("_recipe_builder_summary") or ""
                        ),
                        excluded_ingredients=list(
                            known_data.get("_recipe_import_excluded") or []
                        ),
                    )
            except (TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save that recipe: {exc}\n\n"
                    "Nothing was logged.",
                    chat_id=chat_id,
                )
                return
            except Exception:
                logging.exception("Recipe Builder save failed")
                send_telegram_msg(
                    "I couldn't save that recipe safely. Nothing was "
                    "logged or changed.",
                    chat_id=chat_id,
                )
                return

            recipe = result.get("recipe")
            if not result.get("created"):
                send_telegram_msg(
                    "A Saved Recipe with that name already exists. "
                    "Nothing was overwritten.",
                    chat_id=chat_id,
                )
                return
            created_food_ids = list(
                known_data.get("_recipe_import_created_food_ids") or []
            )
            if known_data.get("_recipe_import_active") and created_food_ids:
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_import_pantry_offer",
                    known_data={
                        "saved_recipe_id": int(recipe["saved_recipe_id"]),
                        "_recipe_import_created_food_ids": created_food_ids,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Saved Recipe created. Nothing was logged.\n\n"
                    "Add the new Saved Food ingredients created during "
                    "this import to My Pantry?\n\n1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_details",
                known_data={
                    "saved_recipe_id": int(recipe["saved_recipe_id"]),
                    "saved_recipe_servings": None,
                    "saved_recipe_meal": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                (
                    "Saved Recipe recalculated for future logs. "
                    "Previous food entries were not changed.\n\n"
                    if existing_recipe_id
                    else "Saved Recipe created. Nothing was logged.\n\n"
                )
                + format_saved_recipe_details(recipe),
                chat_id=chat_id,
            )
            return

        if current_step == "recipe_import_pantry_offer":
            if lowered not in {"1", "yes", "2", "no"}:
                send_telegram_msg(
                    "Add the newly created Saved Foods to My Pantry?\n\n"
                    "1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return
            added = 0
            if lowered in {"1", "yes"}:
                foods_by_id = {
                    int(food["food_id"]): food
                    for food in list_recipe_ingredient_foods()
                }
                for food_id in list(
                    known_data.get("_recipe_import_created_food_ids") or []
                ):
                    food = foods_by_id.get(int(food_id))
                    if food is None:
                        continue
                    result = add_pantry_item(
                        display_name=str(food["canonical_name"]),
                        food_id=int(food_id),
                        source="saved_food",
                    )
                    if result.get("created"):
                        added += 1
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "The recipe was saved, but I couldn't reopen its "
                    "details. Nothing was logged.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_details",
                known_data={
                    "saved_recipe_id": int(recipe["saved_recipe_id"]),
                    "saved_recipe_servings": None,
                    "saved_recipe_meal": None,
                },
                missing_fields=[],
            )
            pantry_note = (
                f"Added {added} new ingredient(s) to My Pantry.\n\n"
                if lowered in {"1", "yes"}
                else "My Pantry was not changed.\n\n"
            )
            send_telegram_msg(
                pantry_note + format_saved_recipe_details(recipe),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_browse":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return

            recipe_ids = list(
                known_data.get("saved_recipe_ids") or []
            )
            try:
                selected_index = int(text.strip()) - 1
                saved_recipe_id = int(recipe_ids[selected_index])
                if selected_index < 0:
                    raise IndexError
                recipe = get_saved_recipe(saved_recipe_id)
            except (IndexError, TypeError, ValueError):
                recipe = None

            if recipe is None:
                recipes = list_saved_recipes()
                send_telegram_msg(
                    format_saved_recipe_choices(recipes),
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_details",
                known_data={
                    "saved_recipe_id": saved_recipe_id,
                    "saved_recipe_servings": None,
                    "saved_recipe_meal": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_saved_recipe_details(recipe),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_details":
            saved_recipe_id = int(
                known_data.get("saved_recipe_id") or 0
            )
            recipe = get_saved_recipe(saved_recipe_id)
            if recipe is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That Saved Recipe is no longer available.\n\n"
                    + healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered == "back":
                recipes = list_saved_recipes()
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_browse",
                    known_data={
                        "saved_recipe_ids": [
                            int(item["saved_recipe_id"])
                            for item in recipes
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_choices(recipes),
                    chat_id=chat_id,
                )
                return

            if lowered in {"log", "log recipe", "log it"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_log_meal",
                    known_data={
                        "saved_recipe_meal": None,
                        "saved_recipe_servings": None,
                    },
                    missing_fields=["meal_category"],
                )
                send_telegram_msg(
                    "Which meal should this recipe be logged under?",
                    chat_id=chat_id,
                )
                return

            if lowered in {"edit", "edit recipe"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_menu",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return

            if lowered in {"delete", "delete recipe"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_delete_confirmation",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Delete this Saved Recipe?\n\n"
                    f"Recipe: {recipe.get('canonical_name')}\n\n"
                    "It will disappear from Saved Recipes. Foods "
                    "already logged from it will remain unchanged.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_saved_recipe_details(recipe),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "saved_recipe_edit_select",
            "saved_recipe_delete_select",
        }:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return

            recipe_ids = list(
                known_data.get("saved_recipe_ids") or []
            )
            try:
                selected_index = int(text.strip()) - 1
                if selected_index < 0:
                    raise IndexError
                saved_recipe_id = int(recipe_ids[selected_index])
                recipe = get_saved_recipe(saved_recipe_id)
            except (IndexError, TypeError, ValueError):
                recipe = None

            action = (
                "edit"
                if current_step == "saved_recipe_edit_select"
                else "delete"
            )
            if recipe is None:
                recipes = list_saved_recipes()
                send_telegram_msg(
                    format_saved_recipe_management_choices(
                        recipes,
                        action=action,
                    ),
                    chat_id=chat_id,
                )
                return

            if action == "edit":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_menu",
                    known_data={
                        "saved_recipe_id": saved_recipe_id,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_delete_confirmation",
                known_data={"saved_recipe_id": saved_recipe_id},
                missing_fields=[],
            )
            send_telegram_msg(
                "Delete this Saved Recipe?\n\n"
                f"Recipe: {recipe.get('canonical_name')}\n\n"
                "It will disappear from Saved Recipes. Foods "
                "already logged from it will remain unchanged.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_edit_menu":
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That Saved Recipe is no longer available.\n\n"
                    + healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"7", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_details(recipe),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "ingredients"}:
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_recipe_builder_existing_id": int(
                            recipe["saved_recipe_id"]
                        ),
                        "_recipe_builder_name": str(
                            recipe.get("canonical_name") or ""
                        ),
                        "_recipe_builder_meal_type": str(
                            recipe.get("meal_type") or "dinner"
                        ),
                        "_recipe_builder_yield": float(
                            recipe.get("yield_servings") or 1
                        ),
                        "_recipe_builder_ingredients": [],
                        "_recipe_builder_summary": str(
                            recipe.get("summary") or ""
                        ),
                        "_recipe_builder_steps": list(
                            recipe.get("preparation_steps") or []
                        ),
                        "_recipe_builder_selected_food_id": None,
                        "_recipe_builder_food_ids": [],
                    },
                    missing_fields=["ingredient"],
                )
                send_telegram_msg(
                    "Recipe Builder — Recalculate Ingredients\n\n"
                    "Re-enter all ingredients and amounts. Saving will "
                    "create a new recipe nutrition version for future "
                    "logs; previous food entries will remain unchanged.",
                    chat_id=chat_id,
                )
                show_recipe_builder_pantry_choices(chat_id=chat_id)
                return

            edit_steps = {
                "1": ("saved_recipe_edit_name", "What should this recipe be called?"),
                "name": ("saved_recipe_edit_name", "What should this recipe be called?"),
                "2": ("saved_recipe_edit_meal_type", "Should this recipe be for lunch or dinner?"),
                "meal type": ("saved_recipe_edit_meal_type", "Should this recipe be for lunch or dinner?"),
                "3": ("saved_recipe_edit_summary", "Enter a short recipe summary. Reply Clear to remove it."),
                "summary": ("saved_recipe_edit_summary", "Enter a short recipe summary. Reply Clear to remove it."),
                "5": (
                    "saved_recipe_edit_preparation",
                    "Enter the preparation steps, one per line.",
                ),
                "preparation": (
                    "saved_recipe_edit_preparation",
                    "Enter the preparation steps, one per line.",
                ),
                "6": (
                    "saved_recipe_edit_nutrition_calories",
                    "Enter the new calories for one serving.",
                ),
                "nutrition": (
                    "saved_recipe_edit_nutrition_calories",
                    "Enter the new calories for one serving.",
                ),
            }
            selection = edit_steps.get(lowered)
            if selection is None:
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step=selection[0],
                known_data={
                    "_saved_recipe_edit_kind": None,
                    "_saved_recipe_edit_value": None,
                    "_saved_recipe_edit_nutrition": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(selection[1], chat_id=chat_id)
            return

        if current_step in {
            "saved_recipe_edit_name",
            "saved_recipe_edit_meal_type",
            "saved_recipe_edit_summary",
            "saved_recipe_edit_ingredients",
            "saved_recipe_edit_preparation",
        }:
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                send_telegram_msg(
                    "That Saved Recipe is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_menu",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return

            try:
                if current_step == "saved_recipe_edit_name":
                    value = text.strip()
                    if not value:
                        raise ValueError("Enter a recipe name.")
                    kind = "name"
                    preview = f"New name: {value}"
                elif current_step == "saved_recipe_edit_meal_type":
                    if lowered not in {"lunch", "dinner"}:
                        raise ValueError("Choose Lunch or Dinner.")
                    value = lowered
                    kind = "meal_type"
                    preview = f"New meal type: {value.title()}"
                elif current_step == "saved_recipe_edit_summary":
                    value = "" if lowered == "clear" else text.strip()
                    kind = "summary"
                    preview = f"New summary: {value or 'none'}"
                elif current_step == "saved_recipe_edit_ingredients":
                    value = parse_saved_recipe_ingredients(text)
                    kind = "ingredients"
                    preview = "New ingredients:\n" + "\n".join(
                        f"- {item['amount']} {item['name']}"
                        for item in value
                    )
                else:
                    value = parse_saved_recipe_steps(text)
                    kind = "preparation_steps"
                    preview = "New preparation:\n" + "\n".join(
                        f"{index}. {step}"
                        for index, step in enumerate(value, start=1)
                    )
            except ValueError as exc:
                send_telegram_msg(str(exc), chat_id=chat_id)
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_edit_confirmation",
                known_data={
                    "_saved_recipe_edit_kind": kind,
                    "_saved_recipe_edit_value": value,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Save this recipe change?\n\n"
                f"Recipe: {recipe.get('canonical_name')}\n"
                f"{preview}\n\n"
                "Previously logged meals will not change.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_edit_confirmation":
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                send_telegram_msg(
                    "That Saved Recipe is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_menu",
                    known_data={
                        "_saved_recipe_edit_kind": None,
                        "_saved_recipe_edit_value": None,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Reply Yes, No, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return

            kind = str(
                known_data.get("_saved_recipe_edit_kind") or ""
            )
            value = known_data.get("_saved_recipe_edit_value")
            allowed = {
                "name",
                "meal_type",
                "summary",
                "ingredients",
                "preparation_steps",
            }
            if kind not in allowed:
                send_telegram_msg(
                    "That edit expired. Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            try:
                updated_recipe = update_saved_recipe(
                    int(recipe["saved_recipe_id"]),
                    **{kind: value},
                )
            except (TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save that change: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_details",
                known_data={
                    "_saved_recipe_edit_kind": None,
                    "_saved_recipe_edit_value": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Saved Recipe updated. Previously logged meals were "
                "not changed."
                + (
                    " The Heart-Healthy Pick label was removed "
                    "because the ingredients changed."
                    if (
                        kind == "ingredients"
                        and recipe.get("heart_healthy_pick")
                    )
                    else ""
                )
                + "\n\n"
                + format_saved_recipe_details(updated_recipe),
                chat_id=chat_id,
            )
            return

        nutrition_edit_steps = {
            "saved_recipe_edit_nutrition_calories": (
                "calories",
                "saved_recipe_edit_nutrition_protein",
                "Enter the new grams of protein.",
            ),
            "saved_recipe_edit_nutrition_protein": (
                "protein_g",
                "saved_recipe_edit_nutrition_carbohydrates",
                "Enter the new grams of carbohydrates.",
            ),
            "saved_recipe_edit_nutrition_carbohydrates": (
                "carbohydrates_g",
                "saved_recipe_edit_nutrition_fat",
                "Enter the new grams of fat.",
            ),
            "saved_recipe_edit_nutrition_fat": (
                "fat_g",
                "saved_recipe_edit_nutrition_fiber",
                "Enter the new grams of fiber.",
            ),
            "saved_recipe_edit_nutrition_fiber": (
                "fiber_g",
                "saved_recipe_edit_nutrition_sugar",
                "Enter the new grams of sugar.",
            ),
            "saved_recipe_edit_nutrition_sugar": (
                "sugar_g",
                "saved_recipe_edit_nutrition_sodium",
                "Enter the new milligrams of sodium.",
            ),
        }
        if current_step in {
            *nutrition_edit_steps,
            "saved_recipe_edit_nutrition_sodium",
        }:
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                send_telegram_msg(
                    "That Saved Recipe is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_menu",
                    known_data={"_saved_recipe_edit_nutrition": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return
            try:
                number = float(text.strip())
            except (TypeError, ValueError):
                number = -1
            if number < 0:
                send_telegram_msg(
                    "Enter zero or a positive number.",
                    chat_id=chat_id,
                )
                return

            staged = dict(
                known_data.get("_saved_recipe_edit_nutrition") or {}
            )
            if current_step == "saved_recipe_edit_nutrition_sodium":
                staged["sodium_mg"] = number
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_nutrition_confirmation",
                    known_data={
                        "_saved_recipe_edit_nutrition": staged,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Save these recipe nutrition changes?\n\n"
                    f"Recipe: {recipe.get('canonical_name')}\n"
                    "New nutrition version: "
                    f"{int(recipe.get('version_number') or 1) + 1}\n\n"
                    f"Calories: {format_display_number(staged['calories'])}\n"
                    f"Protein: {format_display_number(staged['protein_g'])} g\n"
                    "Carbohydrates: "
                    f"{format_display_number(staged['carbohydrates_g'])} g\n"
                    f"Fat: {format_display_number(staged['fat_g'])} g\n"
                    f"Fiber: {format_display_number(staged['fiber_g'])} g\n"
                    f"Sugar: {format_display_number(staged['sugar_g'])} g\n"
                    f"Sodium: {format_display_number(staged['sodium_mg'])} mg\n\n"
                    "Previously logged meals will not change.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            field, next_step, prompt = nutrition_edit_steps[current_step]
            staged[field] = number
            update_conversation(
                chat_id=chat_id,
                current_step=next_step,
                known_data={"_saved_recipe_edit_nutrition": staged},
                missing_fields=[],
            )
            send_telegram_msg(prompt, chat_id=chat_id)
            return

        if current_step == "saved_recipe_edit_nutrition_confirmation":
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                send_telegram_msg(
                    "That Saved Recipe is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_edit_menu",
                    known_data={"_saved_recipe_edit_nutrition": None},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_edit_menu(recipe),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Reply Yes, No, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return
            nutrition = dict(
                known_data.get("_saved_recipe_edit_nutrition") or {}
            )
            required = {
                "calories",
                "protein_g",
                "carbohydrates_g",
                "fat_g",
                "fiber_g",
                "sugar_g",
                "sodium_mg",
            }
            if set(nutrition) != required:
                send_telegram_msg(
                    "That nutrition edit expired. Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            try:
                updated_recipe = update_saved_recipe_nutrition(
                    int(recipe["saved_recipe_id"]),
                    **nutrition,
                )
            except (TypeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save that nutrition: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_details",
                known_data={"_saved_recipe_edit_nutrition": None},
                missing_fields=[],
            )
            send_telegram_msg(
                "Saved Recipe nutrition updated to version "
                f"{updated_recipe.get('version_number')}. Previously "
                "logged meals were not changed."
                + (
                    " The Heart-Healthy Pick label was removed "
                    "because the nutrition changed."
                    if recipe.get("heart_healthy_pick")
                    else ""
                )
                + "\n\n"
                + format_saved_recipe_details(updated_recipe),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_delete_confirmation":
            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipes",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That Saved Recipe is already gone.\n\n"
                    + healthcoach_saved_recipes_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_details(recipe),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "delete"}:
                send_telegram_msg(
                    "Reply Yes, No, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return
            try:
                deleted = delete_saved_recipe(
                    int(recipe["saved_recipe_id"])
                )
            except (RuntimeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't delete that recipe: {exc}",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipes",
                known_data={"saved_recipe_id": None},
                missing_fields=[],
            )
            send_telegram_msg(
                f"Deleted {deleted.get('canonical_name')} from Saved "
                "Recipes. Previously logged meals were not changed.\n\n"
                + healthcoach_saved_recipes_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_log_meal":
            if lowered == "back":
                recipe = get_saved_recipe(
                    int(known_data.get("saved_recipe_id") or 0)
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_recipe_details(recipe or {}),
                    chat_id=chat_id,
                )
                return

            meal_aliases = {
                "before breakfast": "before breakfast",
                "breakfast": "breakfast",
                "morning snack": "school snack",
                "school snack": "school snack",
                "lunch": "lunch",
                "afternoon snack": "afternoon snack",
                "dinner": "dinner",
                "dessert": "dessert",
            }
            meal_category = meal_aliases.get(lowered)
            if meal_category is None:
                send_telegram_msg(
                    "Which meal should this recipe be logged under?",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_log_servings",
                known_data={"saved_recipe_meal": meal_category},
                missing_fields=["servings"],
            )
            send_telegram_msg(
                "How many servings of this saved recipe?\n\n"
                "Choose 0.5, 1, 1.5, or 2.",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_log_servings":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_log_meal",
                    known_data={"saved_recipe_meal": None},
                    missing_fields=["meal_category"],
                )
                send_telegram_msg(
                    "Which meal should this recipe be logged under?",
                    chat_id=chat_id,
                )
                return

            try:
                servings = float(text.strip())
            except (TypeError, ValueError):
                servings = 0
            if servings <= 0 or servings > 4:
                send_telegram_msg(
                    "Enter a serving amount greater than 0 and no "
                    "more than 4.",
                    chat_id=chat_id,
                )
                return

            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            if recipe is None:
                send_telegram_msg(
                    "That Saved Recipe is no longer available.",
                    chat_id=chat_id,
                )
                return
            calories = float(recipe.get("calories") or 0) * servings
            protein = float(recipe.get("protein_g") or 0) * servings
            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipe_log_confirmation",
                known_data={"saved_recipe_servings": servings},
                missing_fields=[],
            )
            send_telegram_msg(
                "Log this saved recipe?\n\n"
                f"Recipe: {recipe.get('canonical_name')}\n"
                f"Meal: {str(known_data.get('saved_recipe_meal') or '').title()}\n"
                f"Servings: {format_display_number(servings)}\n"
                "Estimated calories: "
                f"{format_display_number(calories, decimals=0)}\n"
                "Estimated protein: "
                f"{format_display_number(protein)} g\n\n"
                "1. Log Recipe\n"
                "2. Back\n"
                "3. Cancel",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_recipe_log_confirmation":
            if lowered in {"2", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_recipe_log_servings",
                    known_data={"saved_recipe_servings": None},
                    missing_fields=["servings"],
                )
                send_telegram_msg(
                    "How many servings of this saved recipe?\n\n"
                    "Choose 0.5, 1, 1.5, or 2.",
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "log", "log recipe", "log it"}:
                send_telegram_msg(
                    "Reply Log Recipe, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return

            recipe = get_saved_recipe(
                int(known_data.get("saved_recipe_id") or 0)
            )
            meal_category = str(
                known_data.get("saved_recipe_meal") or ""
            )
            servings = float(
                known_data.get("saved_recipe_servings") or 0
            )
            if recipe is None or not meal_category or servings <= 0:
                send_telegram_msg(
                    "That recipe selection expired. Nothing was logged.",
                    chat_id=chat_id,
                )
                return

            try:
                entry = add_food_entry(
                    entry_date=today,
                    meal_category=meal_category,
                    food_id=int(recipe["food_id"]),
                    quantity=servings,
                    logging_source="recipe",
                    original_text=(
                        "Saved Recipe: "
                        + str(recipe.get("canonical_name") or "Recipe")
                    ),
                    quantity_is_estimated=True,
                    user_confirmed=True,
                )
            except Exception:
                logging.exception("Saved Recipe logging failed")
                send_telegram_msg(
                    "I couldn't log that Saved Recipe safely. "
                    "Nothing was added to today's food log.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(today)
            except Exception:
                logging.exception(
                    "Saved Recipe Google Sheet sync failed"
                )
                sync_note = (
                    "\n\nThe recipe was logged, but the Google "
                    "Sheet totals could not be updated."
                )
            else:
                sync_note = ""

            update_conversation(
                chat_id=chat_id,
                current_step="saved_recipes",
                known_data={
                    "saved_recipe_id": None,
                    "saved_recipe_meal": None,
                    "saved_recipe_servings": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                append_goal_calorie_progress(
                    "Saved Recipe logged.\n\n"
                    f"Recipe: {recipe.get('canonical_name')}\n"
                    f"Meal: {meal_category.title()}\n"
                    "Calories: "
                    f"{format_display_number(float(entry.get('calories') or 0), decimals=0)}\n"
                    "Protein: "
                    f"{format_display_number(float(entry.get('protein_g') or 0))} g"
                    + sync_note,
                    today,
                )
                + "\n\n"
                + healthcoach_saved_recipes_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_foods":
            if lowered in {
                "1",
                "find",
                "find a food",
                "search",
                "search foods",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_finder_query",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Food Finder\n\n"
                    "What food would you like to find?\n\n"
                    "Search by food name, brand, or a name you used before. "
                    "For example: homemade protein bars.\n\n"
                    "Reply Back to return or Cancel to close.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "2",
                "browse",
                "browse saved foods",
                "browse entered foods",
                "entered foods",
            }:
                foods = list_user_saved_foods()

                if not foods:
                    send_telegram_msg(
                        "There are no manually entered foods yet. Use Find "
                        "a food to search every reusable nutrition record.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_browse",
                    known_data={
                        "_saved_food_ids": [
                            int(food["food_id"])
                            for food in foods
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_choices(foods),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "3",
                "add",
                "add saved food",
                "add entered food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_add_name",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What should this saved food be called?\n\n"
                    "Example: Turkey Fiesta Bowl",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "4",
                "edit",
                "edit saved food",
                "edit entered food",
            }:
                foods = list_user_saved_foods()

                if not foods:
                    send_telegram_msg(
                        "There are no manually saved foods to edit.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_select",
                    known_data={
                        "_saved_food_ids": [
                            int(food["food_id"])
                            for food in foods
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_edit_choices(foods),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "5",
                "delete",
                "delete saved food",
                "remove entered food",
            }:
                foods = list_user_saved_foods()

                if not foods:
                    send_telegram_msg(
                        "There are no manually saved foods to delete.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_delete_select",
                    known_data={
                        "_saved_food_ids": [
                            int(food["food_id"])
                            for food in foods
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_delete_choices(foods),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "6",
                "review possible duplicates",
                "review duplicates",
                "duplicate cleanup",
                "cleanup",
            }:
                show_food_duplicate_list(chat_id=chat_id)
                return

            if lowered in {
                "7",
                "library health check",
                "food library health check",
                "health check",
            }:
                show_food_library_health_check(chat_id=chat_id)
                return

            if lowered in {"8", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_saved_foods_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "food_health_check":
            report = build_food_library_health_check()
            if lowered in {"6", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "1",
                "complete pantry nutrition",
                "pantry nutrition",
            }:
                if not report.get("pantry_nutrition"):
                    send_telegram_msg(
                        "Pantry nutrition is already complete.",
                        chat_id=chat_id,
                    )
                    show_food_library_health_check(chat_id=chat_id)
                    return
                show_pantry_nutrition_queue(
                    chat_id=chat_id,
                    page=0,
                    skipped_ids=[],
                )
                return
            if lowered in {"2", "organize pantry", "pantry organization"}:
                if not report.get("pantry_organization"):
                    send_telegram_msg(
                        "Every Pantry item already has a storage location "
                        "and food type.",
                        chat_id=chat_id,
                    )
                    show_food_library_health_check(chat_id=chat_id)
                    return
                show_pantry_organizer_page(chat_id=chat_id, page=0)
                return
            if lowered in {
                "3",
                "review nutrition gaps",
                "nutrition gaps",
            }:
                show_food_health_check_list(
                    chat_id=chat_id,
                    category="nutrition_gaps",
                )
                return
            if lowered in {"4", "review sources", "source review"}:
                show_food_health_check_list(
                    chat_id=chat_id,
                    category="source_review",
                )
                return
            if lowered in {
                "5",
                "review source rechecks",
                "source rechecks",
                "rechecks",
            }:
                show_food_health_check_list(
                    chat_id=chat_id,
                    category="source_rechecks",
                )
                return
            send_telegram_msg(
                format_food_library_health_check(report),
                chat_id=chat_id,
            )
            return

        if current_step == "food_health_check_list":
            category = str(
                known_data.get("food_health_check_category") or ""
            )
            if category not in FOOD_HEALTH_CHECK_LABELS:
                show_food_library_health_check(chat_id=chat_id)
                return
            page = int(known_data.get("food_health_check_page") or 0)
            if lowered == "back":
                show_food_library_health_check(chat_id=chat_id)
                return
            if lowered == "next":
                show_food_health_check_list(
                    chat_id=chat_id,
                    category=category,
                    page=page + 1,
                )
                return
            if lowered == "previous":
                show_food_health_check_list(
                    chat_id=chat_id,
                    category=category,
                    page=page - 1,
                )
                return
            ids = list(known_data.get("food_health_check_ids") or [])
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(ids):
                send_telegram_msg(
                    "Choose one of the numbered foods, Next, Previous, "
                    "Back, or Cancel.",
                    chat_id=chat_id,
                )
                return
            food_id = int(ids[selection - 1])
            latest_report = build_food_library_health_check()
            selected = next(
                (
                    food
                    for food in health_check_food_items(
                        latest_report,
                        category,
                    )
                    if int(food.get("food_id") or 0) == food_id
                ),
                None,
            )
            if selected is None:
                send_telegram_msg(
                    "That finding changed since this list was opened. The "
                    "health check has been refreshed.",
                    chat_id=chat_id,
                )
                show_food_health_check_list(
                    chat_id=chat_id,
                    category=category,
                    page=page,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="food_health_check_item",
                known_data={
                    "food_health_check_category": category,
                    "food_health_check_page": page,
                    "food_health_check_food_id": food_id,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_food_health_check_item(selected, category=category),
                chat_id=chat_id,
            )
            return

        if current_step == "food_health_check_item":
            category = str(
                known_data.get("food_health_check_category") or ""
            )
            if category not in FOOD_HEALTH_CHECK_LABELS:
                show_food_library_health_check(chat_id=chat_id)
                return
            page = int(known_data.get("food_health_check_page") or 0)
            food_id = int(known_data.get("food_health_check_food_id") or 0)
            if lowered == "back":
                show_food_health_check_list(
                    chat_id=chat_id,
                    category=category,
                    page=page,
                )
                return
            if lowered in {"manage food", "manage", "edit food"}:
                show_food_library_manage(
                    chat_id=chat_id,
                    food_id=food_id,
                    return_data={
                        "_food_health_check_category": category,
                        "_food_health_check_page": page,
                    },
                )
                return
            latest_report = build_food_library_health_check()
            selected = next(
                (
                    food
                    for food in health_check_food_items(
                        latest_report,
                        category,
                    )
                    if int(food.get("food_id") or 0) == food_id
                ),
                None,
            )
            if selected is None:
                show_food_health_check_list(
                    chat_id=chat_id,
                    category=category,
                    page=page,
                )
                return
            send_telegram_msg(
                format_food_health_check_item(selected, category=category),
                chat_id=chat_id,
            )
            return

        if current_step == "food_duplicate_list":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return
            pair_ids = list(known_data.get("food_duplicate_pair_ids") or [])
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(pair_ids):
                send_telegram_msg(
                    "Choose one of the numbered possible duplicates, Back, "
                    "or Cancel.",
                    chat_id=chat_id,
                )
                return
            selected_pair = list(pair_ids[selection - 1])
            if len(selected_pair) != 2:
                show_food_duplicate_list(chat_id=chat_id)
                return
            first = get_food_location(int(selected_pair[0]))
            second = get_food_location(int(selected_pair[1]))
            if first is None or second is None:
                send_telegram_msg(
                    "One record changed since this list was opened. The "
                    "possible-duplicate list has been refreshed.",
                    chat_id=chat_id,
                )
                show_food_duplicate_list(chat_id=chat_id)
                return
            update_conversation(
                chat_id=chat_id,
                current_step="food_duplicate_compare",
                known_data={
                    "food_duplicate_first_id": int(first["food_id"]),
                    "food_duplicate_second_id": int(second["food_id"]),
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_food_duplicate_comparison(first, second),
                chat_id=chat_id,
            )
            return

        if current_step == "food_duplicate_compare":
            if lowered in {"4", "back"}:
                show_food_duplicate_list(chat_id=chat_id)
                return
            first_id = int(known_data.get("food_duplicate_first_id") or 0)
            second_id = int(known_data.get("food_duplicate_second_id") or 0)
            first = get_food_location(first_id)
            second = get_food_location(second_id)
            if first is None or second is None:
                send_telegram_msg(
                    "One record is no longer available. The cleanup list "
                    "has been refreshed.",
                    chat_id=chat_id,
                )
                show_food_duplicate_list(chat_id=chat_id)
                return
            if lowered in {"3", "keep both", "different", "separate"}:
                try:
                    mark_foods_keep_separate(first_id, second_id)
                except Exception:
                    logging.exception("Could not save duplicate review")
                    send_telegram_msg(
                        "I could not save that review. Nothing was changed.",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(
                    "Kept both Food Library records. They will not be "
                    "suggested as duplicates again.",
                    chat_id=chat_id,
                )
                show_food_duplicate_list(chat_id=chat_id)
                return
            if lowered in {"1", "use first", "first"}:
                primary, duplicate = first, second
            elif lowered in {"2", "use second", "second"}:
                primary, duplicate = second, first
            else:
                send_telegram_msg(
                    "Choose Use first, Use second, Keep both, Back, or "
                    "Cancel.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="food_duplicate_confirmation",
                known_data={
                    "food_duplicate_primary_id": int(primary["food_id"]),
                    "food_duplicate_retired_id": int(duplicate["food_id"]),
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Consolidate these Food Library records?\n\n"
                "Keep for future use:\n"
                f"- {_food_duplicate_identity(primary)}\n"
                f"- Serving: {primary.get('serving_description')}\n"
                f"- Source: {primary.get('verification_source')}\n\n"
                "Retire from future choices:\n"
                f"- {_food_duplicate_identity(duplicate)}\n\n"
                "Pantry links, Favorites, barcodes, portion phrases, and "
                "search names will point to the kept record. Past logs, all "
                "Nutrition Versions, and existing saved recipe calculations "
                "will remain attached to their original records.\n\n"
                "Nothing has changed yet.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "food_duplicate_confirmation":
            primary_id = int(
                known_data.get("food_duplicate_primary_id") or 0
            )
            duplicate_id = int(
                known_data.get("food_duplicate_retired_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                first = get_food_location(
                    int(known_data.get("food_duplicate_first_id") or 0)
                )
                second = get_food_location(
                    int(known_data.get("food_duplicate_second_id") or 0)
                )
                if first is None or second is None:
                    show_food_duplicate_list(chat_id=chat_id)
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_duplicate_compare",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Nothing was consolidated.\n\n"
                    + format_food_duplicate_comparison(first, second),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "confirm", "consolidate"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return
            try:
                result = consolidate_food_records(
                    primary_food_id=primary_id,
                    duplicate_food_id=duplicate_id,
                )
            except Exception as exc:
                logging.exception("Food Library consolidation failed")
                send_telegram_msg(
                    "I could not consolidate those records safely. Nothing "
                    f"was changed.\n\nReason: {exc}",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                "Food Library records consolidated.\n\n"
                f"Future record: {result['primary_name']}\n"
                f"Retired duplicate: {result['duplicate_name']}\n\n"
                "Past logs, nutrition history, and saved recipe "
                "calculations were preserved.",
                chat_id=chat_id,
            )
            show_food_duplicate_list(chat_id=chat_id)
            return

        if current_step == "food_finder_query":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return
            show_food_finder_results(
                chat_id=chat_id,
                query=text,
            )
            return

        if current_step == "food_finder_results":
            if lowered in {"search again", "new search", "search"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_finder_query",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Food Finder\n\n"
                    "What food would you like to find?\n\n"
                    "Search by food name, brand, or a name you used before.\n\n"
                    "Reply Back to return or Cancel to close.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            food_ids = list(known_data.get("food_finder_ids") or [])
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(food_ids):
                send_telegram_msg(
                    "Choose one of the numbered Food Finder results, "
                    "Search again, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return

            food = get_food_location(int(food_ids[selection - 1]))
            if food is None:
                send_telegram_msg(
                    "That Food Library record is no longer available. "
                    "Search again to refresh the results.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="food_finder_details",
                known_data={
                    **known_data,
                    "food_finder_food_id": int(food["food_id"]),
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_food_finder_details(food),
                chat_id=chat_id,
            )
            return

        if current_step == "food_finder_details":
            if lowered in {"manage food", "manage", "edit food"}:
                show_food_library_manage(
                    chat_id=chat_id,
                    food_id=int(
                        known_data.get("food_finder_food_id") or 0
                    ),
                )
                return
            if lowered in {"search again", "new search", "search"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_finder_query",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Food Finder\n\n"
                    "What food would you like to find?\n\n"
                    "Search by food name, brand, or a name you used before.\n\n"
                    "Reply Back to return or Cancel to close.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            if lowered == "back":
                show_food_finder_results(
                    chat_id=chat_id,
                    query=str(
                        known_data.get("food_finder_query") or "food"
                    ),
                )
                return
            send_telegram_msg(
                format_food_finder_details(
                    get_food_location(
                        int(known_data.get("food_finder_food_id") or 0)
                    )
                    or {}
                ),
                chat_id=chat_id,
            )
            return

        if current_step == "food_library_manage":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            food = get_food_location(food_id)
            if food is None:
                send_telegram_msg(
                    "That Food Library record is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"5", "back"}:
                health_category = str(
                    known_data.get("_food_health_check_category") or ""
                )
                if health_category in FOOD_HEALTH_CHECK_LABELS:
                    health_page = int(
                        known_data.get("_food_health_check_page") or 0
                    )
                    report = build_food_library_health_check()
                    health_food = next(
                        (
                            item
                            for item in health_check_food_items(
                                report,
                                health_category,
                            )
                            if int(item.get("food_id") or 0) == food_id
                        ),
                        None,
                    )
                    if health_food is None:
                        show_food_health_check_list(
                            chat_id=chat_id,
                            category=health_category,
                            page=health_page,
                        )
                        return
                    update_conversation(
                        chat_id=chat_id,
                        current_step="food_health_check_item",
                        known_data={
                            "food_health_check_category": health_category,
                            "food_health_check_page": health_page,
                            "food_health_check_food_id": food_id,
                        },
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        format_food_health_check_item(
                            health_food,
                            category=health_category,
                        ),
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_finder_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_food_finder_details(food),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "1",
                "change name",
                "add a personal search name",
                "personal search name",
            }:
                is_alias = not bool(food.get("is_entered_food"))
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_library_name",
                    known_data={"food_library_name_is_alias": is_alias},
                    missing_fields=[],
                )
                if is_alias:
                    prompt = (
                        f"Verified name: {food['canonical_name']}\n\n"
                        "What personal name should also find this food?\n\n"
                        "The verified source name will remain unchanged."
                    )
                else:
                    prompt = (
                        f"Current name: {food['canonical_name']}\n\n"
                        "What should this entered food be called?"
                    )
                send_telegram_msg(
                    prompt + "\n\nReply Back to return without changing it.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            if lowered in {
                "2",
                "pantry location and type",
                "manage pantry location and type",
                "add to pantry",
            }:
                pantry_items = finder_pantry_items(food_id)
                if not pantry_items:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="food_library_add_pantry_confirmation",
                        known_data={},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        "Add this food to My Pantry?\n\n"
                        f"Food: {food['canonical_name']}\n"
                        "Storage and food type will start as Unsorted, and "
                        "you can organize them next.\n\n"
                        "The existing nutrition will be linked. Nothing "
                        "will be logged as eaten.\n\n"
                        "1. Yes\n"
                        "2. No",
                        chat_id=chat_id,
                    )
                    return
                if len(pantry_items) == 1:
                    show_food_library_pantry_item(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            pantry_items[0]["pantry_item_id"]
                        ),
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_library_pantry_select",
                    known_data={
                        "food_library_pantry_ids": [
                            int(item["pantry_item_id"])
                            for item in pantry_items
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_food_library_pantry_choices(pantry_items),
                    chat_id=chat_id,
                )
                return
            if lowered in {"3", "change nutrition", "nutrition"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_library_nutrition_serving",
                    known_data={
                        "_food_finder_return": True,
                        "_saved_food_edit_id": food_id,
                        "_saved_food_edit_name": str(
                            food.get("canonical_name") or "Food"
                        ),
                        "_saved_food_edit_serving": str(
                            food.get("serving_description") or "1 serving"
                        ),
                        "_saved_food_edit_old_version": int(
                            food.get("version_number") or 1
                        ),
                    },
                    missing_fields=[],
                )
                source_note = (
                    "This creates a user-entered nutrition version for "
                    "future use. The original source version and all past "
                    "logs remain preserved.\n\n"
                    if not food.get("is_entered_food")
                    else "This creates a new nutrition version for future "
                    "use. Past logs remain unchanged.\n\n"
                )
                send_telegram_msg(
                    f"Changing nutrition for {food['canonical_name']}\n"
                    f"Serving: {food.get('serving_description') or '1 serving'}\n\n"
                    + source_note
                    + "What should one serving be for future logs?\n\n"
                    "Example: 1 half bar (26.5 g)\n\n"
                    "Reply Keep current to leave the serving unchanged, "
                    "or Back to return.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return
            if lowered in {
                "4",
                "remove or archive",
                "review removal options",
                "remove",
                "delete",
            }:
                pantry_items = finder_pantry_items(food_id)
                if pantry_items:
                    if len(pantry_items) == 1:
                        show_food_library_pantry_item(
                            chat_id=chat_id,
                            pantry_item_id=int(
                                pantry_items[0]["pantry_item_id"]
                            ),
                        )
                    else:
                        update_conversation(
                            chat_id=chat_id,
                            current_step="food_library_pantry_select",
                            known_data={
                                "food_library_pantry_ids": [
                                    int(item["pantry_item_id"])
                                    for item in pantry_items
                                ],
                            },
                            missing_fields=[],
                        )
                        send_telegram_msg(
                            "Choose which Pantry placement to manage.\n\n"
                            + format_food_library_pantry_choices(
                                pantry_items
                            ),
                            chat_id=chat_id,
                        )
                    return
                if food.get("is_entered_food"):
                    if (
                        food.get("is_saved_recipe")
                        or int(food.get("favorite_count") or 0)
                        or int(food.get("barcode_count") or 0)
                    ):
                        send_telegram_msg(
                            "This entered food is still used by a Saved "
                            "Recipe, Favorite, or saved barcode, so it cannot "
                            "be archived yet. Remove those active uses first. "
                            "Past food logs never need to be deleted.",
                            chat_id=chat_id,
                        )
                        return
                    update_conversation(
                        chat_id=chat_id,
                        current_step="food_library_archive_confirmation",
                        known_data={},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        "Archive this entered food?\n\n"
                        f"Food: {food['canonical_name']}\n\n"
                        "It will leave the Entered Foods list. Past logs "
                        "and nutrition history will remain available.\n\n"
                        "Nothing has been changed yet.\n\n"
                        "1. Yes\n"
                        "2. No",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(
                    "This is a verified source or historical nutrition "
                    "record, not an active personal Pantry or Entered Food "
                    "item. There is nothing personal to delete. Keeping the "
                    "record protects past logs and prevents nutrition from "
                    "being silently lost.",
                    chat_id=chat_id,
                )
                return
            show_food_library_manage(chat_id=chat_id, food_id=food_id)
            return

        if current_step == "food_library_nutrition_serving":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            if lowered == "back":
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            if lowered in {"keep", "keep current", "same", "no change"}:
                serving = str(
                    known_data.get("_saved_food_edit_serving")
                    or "1 serving"
                )
            else:
                serving = " ".join(text.strip().split())
                try:
                    parse_serving_description(serving)
                except ValueError:
                    send_telegram_msg(
                        "Describe one serving with a number and unit.\n\n"
                        "Examples: 1 half bar (26.5 g), 1 bowl, or 3 oz.\n"
                        "Reply Keep current or Back.",
                        chat_id=chat_id,
                    )
                    return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_edit_calories",
                known_data={"_saved_food_edit_serving": serving},
                missing_fields=[],
            )
            send_telegram_msg(
                f"Serving for future logs: {serving}\n\n"
                "Enter the calories for that serving.",
                chat_id=chat_id,
                remove_keyboard=True,
            )
            return

        if current_step == "food_library_name":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            if lowered == "back":
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            revised_name = " ".join(text.strip().split())
            if len(revised_name) < 2:
                send_telegram_msg(
                    "Enter a name with at least two characters.",
                    chat_id=chat_id,
                )
                return
            food = get_food_location(food_id)
            if food is None:
                send_telegram_msg(
                    "That Food Library record is no longer available.",
                    chat_id=chat_id,
                )
                return
            is_alias = bool(known_data.get("food_library_name_is_alias"))
            update_conversation(
                chat_id=chat_id,
                current_step="food_library_name_confirmation",
                known_data={"food_library_new_name": revised_name},
                missing_fields=[],
            )
            send_telegram_msg(
                ("Add this personal search name?" if is_alias else
                 "Save this Food Library name change?")
                + "\n\n"
                f"Current name: {food['canonical_name']}\n"
                f"New name: {revised_name}\n\n"
                + (
                    "The verified source name remains unchanged.\n\n"
                    if is_alias else
                    "Nutrition and past logs remain unchanged.\n\n"
                )
                + "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "food_library_name_confirmation":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was changed.", chat_id=chat_id)
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            revised_name = str(known_data.get("food_library_new_name") or "")
            try:
                if known_data.get("food_library_name_is_alias"):
                    save_food_alias(food_id=food_id, alias_text=revised_name)
                    result_text = (
                        f"Added {revised_name} as a personal search name. "
                        "The verified source name was preserved."
                    )
                else:
                    update_user_saved_food_identity(
                        food_id=food_id,
                        canonical_name=revised_name,
                    )
                    result_text = (
                        f"Changed the entered food name to {revised_name}. "
                        "Nutrition and past logs were preserved."
                    )
            except (RuntimeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save that name: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(result_text, chat_id=chat_id)
            show_food_library_manage(chat_id=chat_id, food_id=food_id)
            return

        if current_step == "food_library_add_pantry_confirmation":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was added.", chat_id=chat_id)
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            if lowered not in {"1", "yes", "add"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            food = get_food_location(food_id)
            if food is None:
                send_telegram_msg(
                    "That Food Library record is no longer available.",
                    chat_id=chat_id,
                )
                return
            same_name = next(
                (
                    item for item in list_pantry_items()
                    if str(item.get("display_name") or "").casefold()
                    == str(food["canonical_name"]).casefold()
                    and item.get("food_id") is not None
                    and int(item["food_id"]) != food_id
                ),
                None,
            )
            if same_name is not None:
                send_telegram_msg(
                    "A different linked Pantry item already uses that "
                    "name. Rename that Pantry item first so nothing is "
                    "silently relinked. Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            try:
                item = add_pantry_item(
                    display_name=str(food["canonical_name"]),
                    food_id=food_id,
                    source="saved_food",
                )
            except ValueError as exc:
                send_telegram_msg(
                    f"I couldn't add that Pantry item: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                f"Added {item['display_name']} to My Pantry with its "
                "existing nutrition linked. Nothing was logged as eaten.",
                chat_id=chat_id,
            )
            show_food_library_pantry_item(
                chat_id=chat_id,
                pantry_item_id=int(item["pantry_item_id"]),
            )
            return

        if current_step == "food_library_pantry_select":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            if lowered == "back":
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            pantry_ids = list(known_data.get("food_library_pantry_ids") or [])
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(pantry_ids):
                send_telegram_msg(
                    "Choose one of the numbered Pantry items or reply Back.",
                    chat_id=chat_id,
                )
                return
            show_food_library_pantry_item(
                chat_id=chat_id,
                pantry_item_id=int(pantry_ids[selection - 1]),
            )
            return

        if current_step == "food_library_pantry_item":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            pantry_item_id = int(
                known_data.get("food_library_pantry_item_id") or 0
            )
            item = get_pantry_item_by_id(pantry_item_id)
            if item is None:
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            if lowered in {"3", "back"}:
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            if lowered in {
                "1",
                "change location and type",
                "change storage and food type",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_library_pantry_storage",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_pantry_storage_choices(item["display_name"]),
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "remove from pantry", "remove", "delete"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food_library_pantry_remove_confirmation",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Remove this item from My Pantry?\n\n"
                    f"Item: {item['display_name']}\n\n"
                    "Its Food Library nutrition, recipes, Favorites, and "
                    "past logs will be preserved.\n\n"
                    "Nothing has been removed yet.\n\n"
                    "1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return
            show_food_library_pantry_item(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
            )
            return

        if current_step == "food_library_pantry_storage":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            pantry_item_id = int(
                known_data.get("food_library_pantry_item_id") or 0
            )
            if lowered == "back":
                show_food_library_pantry_item(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                )
                return
            storage = pantry_choice_key(text, PANTRY_STORAGE_AREAS)
            if storage is None:
                send_telegram_msg(
                    "Choose one of the listed storage locations or Back.",
                    chat_id=chat_id,
                )
                return
            item = get_pantry_item_by_id(pantry_item_id)
            if item is None:
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            update_conversation(
                chat_id=chat_id,
                current_step="food_library_pantry_category",
                known_data={"food_library_pantry_storage": storage},
                missing_fields=[],
            )
            send_telegram_msg(
                format_pantry_category_choices(item["display_name"]),
                chat_id=chat_id,
            )
            return

        if current_step == "food_library_pantry_category":
            pantry_item_id = int(
                known_data.get("food_library_pantry_item_id") or 0
            )
            if lowered == "back":
                item = get_pantry_item_by_id(pantry_item_id)
                if item is not None:
                    send_telegram_msg(
                        format_pantry_storage_choices(item["display_name"]),
                        chat_id=chat_id,
                    )
                    update_conversation(
                        chat_id=chat_id,
                        current_step="food_library_pantry_storage",
                        known_data={},
                        missing_fields=[],
                    )
                return
            category = pantry_choice_key(text, PANTRY_FOOD_CATEGORIES)
            if category is None:
                send_telegram_msg(
                    "Choose one of the listed food types or Back.",
                    chat_id=chat_id,
                )
                return
            item = get_pantry_item_by_id(pantry_item_id)
            if item is None:
                show_food_library_manage(
                    chat_id=chat_id,
                    food_id=int(known_data.get("food_finder_food_id") or 0),
                )
                return
            storage = str(known_data.get("food_library_pantry_storage"))
            update_conversation(
                chat_id=chat_id,
                current_step="food_library_pantry_organization_confirmation",
                known_data={"food_library_pantry_category": category},
                missing_fields=[],
            )
            send_telegram_msg(
                "Save this Pantry organization?\n\n"
                f"Item: {item['display_name']}\n"
                f"Storage: {PANTRY_STORAGE_AREAS[storage]}\n"
                f"Food type: {PANTRY_FOOD_CATEGORIES[category]}\n\n"
                "Nothing has been changed yet.\n\n"
                "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "food_library_pantry_organization_confirmation":
            pantry_item_id = int(
                known_data.get("food_library_pantry_item_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was changed.", chat_id=chat_id)
                show_food_library_pantry_item(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                )
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            try:
                update_pantry_item_organization(
                    pantry_item_id,
                    storage_area=str(
                        known_data["food_library_pantry_storage"]
                    ),
                    food_category=str(
                        known_data["food_library_pantry_category"]
                    ),
                )
            except (KeyError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save that organization: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                "Pantry location and food type updated.",
                chat_id=chat_id,
            )
            show_food_library_pantry_item(
                chat_id=chat_id,
                pantry_item_id=pantry_item_id,
            )
            return

        if current_step == "food_library_pantry_remove_confirmation":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            pantry_item_id = int(
                known_data.get("food_library_pantry_item_id") or 0
            )
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was removed.", chat_id=chat_id)
                show_food_library_pantry_item(
                    chat_id=chat_id,
                    pantry_item_id=pantry_item_id,
                )
                return
            if lowered not in {"1", "yes", "remove"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            removed = remove_pantry_item(pantry_item_id)
            send_telegram_msg(
                "Removed the item from My Pantry. Its Food Library "
                "nutrition and history were preserved."
                if removed else
                "That Pantry item was already removed.",
                chat_id=chat_id,
            )
            show_food_library_manage(chat_id=chat_id, food_id=food_id)
            return

        if current_step == "food_library_archive_confirmation":
            food_id = int(known_data.get("food_finder_food_id") or 0)
            if lowered in {"2", "no", "back"}:
                send_telegram_msg("Nothing was changed.", chat_id=chat_id)
                show_food_library_manage(chat_id=chat_id, food_id=food_id)
                return
            if lowered not in {"1", "yes", "archive"}:
                send_telegram_msg("Please choose Yes or No.", chat_id=chat_id)
                return
            try:
                archived = archive_user_saved_food(food_id)
            except ValueError as exc:
                send_telegram_msg(
                    f"I couldn't archive that food: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            send_telegram_msg(
                f"Archived {archived['canonical_name']} from Entered Foods. "
                "Past logs and nutrition history were preserved.",
                chat_id=chat_id,
            )
            show_food_library_manage(chat_id=chat_id, food_id=food_id)
            return

        if current_step == "saved_food_edit_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            food_ids = list(
                known_data.get("_saved_food_ids") or []
            )

            try:
                selection = int(lowered)
            except ValueError:
                selection = 0

            if selection < 1 or selection > len(food_ids):
                send_telegram_msg(
                    "Choose one of the numbered saved foods, "
                    "or reply Back.",
                    chat_id=chat_id,
                )
                return

            food_id = int(food_ids[selection - 1])
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )

            if food is None:
                send_telegram_msg(
                    "That saved food is no longer available.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_edit_menu",
                known_data={
                    "_saved_food_edit_id": food_id,
                    "_saved_food_edit_name": (
                        food["canonical_name"]
                    ),
                    "_saved_food_edit_serving": (
                        food.get("serving_description")
                        or "1 serving"
                    ),
                    "_saved_food_edit_old_version": int(
                        food.get("version_number") or 1
                    ),
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_saved_food_edit_menu(food),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_edit_menu":
            food_id = int(
                known_data.get("_saved_food_edit_id") or 0
            )
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )
            if food is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That Saved Food is no longer available.\n\n"
                    + healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_details(food),
                    chat_id=chat_id,
                )
                return

            if lowered in {"1", "name"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_name",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What should this Saved Food be called?",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "2",
                "serving",
                "serving description",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_serving_description",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "How should one serving be described?\n\n"
                    "Examples: 1 home salad, 1 bowl, or 12 fl oz",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "nutrition"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_calories",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    f"Editing nutrition for {food['canonical_name']}\n"
                    "Serving: "
                    f"{food.get('serving_description') or '1 serving'}\n\n"
                    "Current calories: "
                    f"{format_display_number(float(food.get('calories') or 0))}\n\n"
                    "Enter the new calories for one serving.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            send_telegram_msg(
                format_saved_food_edit_menu(food),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "saved_food_edit_name",
            "saved_food_edit_serving_description",
        }:
            food_id = int(
                known_data.get("_saved_food_edit_id") or 0
            )
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )
            if food is None:
                send_telegram_msg(
                    "That Saved Food is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_menu",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_edit_menu(food),
                    chat_id=chat_id,
                )
                return

            value = text.strip()
            if current_step == "saved_food_edit_name":
                if len(value) < 2:
                    send_telegram_msg(
                        "Enter a food name with at least two characters.",
                        chat_id=chat_id,
                    )
                    return
                kind = "canonical_name"
                preview = f"New name: {value}"
            else:
                if not value:
                    send_telegram_msg(
                        "Enter a serving description.",
                        chat_id=chat_id,
                    )
                    return
                kind = "serving_description"
                preview = f"New serving: {value}"

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_identity_confirmation",
                known_data={
                    "_saved_food_identity_kind": kind,
                    "_saved_food_identity_value": value,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Save this Saved Food change?\n\n"
                f"Food: {food.get('canonical_name')}\n"
                f"{preview}\n\n"
                "Nutrition and previously logged entries will not "
                "change.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_identity_confirmation":
            food_id = int(
                known_data.get("_saved_food_edit_id") or 0
            )
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )
            if food is None:
                send_telegram_msg(
                    "That Saved Food is no longer available.",
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_menu",
                    known_data={
                        "_saved_food_identity_kind": None,
                        "_saved_food_identity_value": None,
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_edit_menu(food),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Reply Yes, No, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return

            kind = str(
                known_data.get("_saved_food_identity_kind") or ""
            )
            value = str(
                known_data.get("_saved_food_identity_value") or ""
            )
            if kind not in {"canonical_name", "serving_description"}:
                send_telegram_msg(
                    "That edit expired. Nothing was changed.",
                    chat_id=chat_id,
                )
                return
            try:
                updated_food = update_user_saved_food_identity(
                    food_id=food_id,
                    **{kind: value},
                )
            except (RuntimeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't save that change: {exc}\n\n"
                    "Nothing was changed.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_details",
                known_data={
                    "_saved_food_edit_name": updated_food["canonical_name"],
                    "_saved_food_edit_serving": updated_food[
                        "serving_description"
                    ],
                    "_saved_food_identity_kind": None,
                    "_saved_food_identity_value": None,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Saved Food updated. Nutrition and previously logged "
                "entries were not changed.\n\n"
                + format_saved_food_details(updated_food),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "saved_food_edit_calories",
            "saved_food_edit_protein",
            "saved_food_edit_carbohydrates",
            "saved_food_edit_fat",
            "saved_food_edit_fiber",
            "saved_food_edit_sugar",
            "saved_food_edit_sodium",
        }:
            if lowered == "back":
                if known_data.get("_food_finder_return"):
                    show_food_library_manage(
                        chat_id=chat_id,
                        food_id=int(
                            known_data.get("food_finder_food_id") or 0
                        ),
                    )
                    return
                if known_data.get(
                    "_pantry_organizer_nutrition_return"
                ):
                    show_pantry_organize_nutrition(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            known_data.get("pantry_organize_id") or 0
                        ),
                        page=int(known_data.get("pantry_page") or 0),
                    )
                    return
                food_id = int(
                    known_data.get("_saved_food_edit_id") or 0
                )
                food = next(
                    (
                        item for item in list_user_saved_foods()
                        if int(item["food_id"]) == food_id
                    ),
                    None,
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_menu",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_edit_menu(food or {}),
                    chat_id=chat_id,
                )
                return

            try:
                number = parse_nutrition_edit_number(
                    text,
                    allow_unavailable=(
                        current_step != "saved_food_edit_calories"
                    ),
                )
            except ValueError as exc:
                send_telegram_msg(
                    str(exc),
                    chat_id=chat_id,
                )
                return

            field_steps = {
                "saved_food_edit_calories": (
                    "_saved_food_edit_calories",
                    "saved_food_edit_protein",
                    "Enter the new grams of protein, or reply Unknown.",
                ),
                "saved_food_edit_protein": (
                    "_saved_food_edit_protein_g",
                    "saved_food_edit_carbohydrates",
                    "Enter the new grams of carbohydrates, or reply Unknown.",
                ),
                "saved_food_edit_carbohydrates": (
                    "_saved_food_edit_carbohydrates_g",
                    "saved_food_edit_fat",
                    "Enter the new grams of fat, or reply Unknown.",
                ),
                "saved_food_edit_fat": (
                    "_saved_food_edit_fat_g",
                    "saved_food_edit_fiber",
                    "Enter the new grams of fiber, or reply Unknown.",
                ),
                "saved_food_edit_fiber": (
                    "_saved_food_edit_fiber_g",
                    "saved_food_edit_sugar",
                    "Enter the new grams of sugar, or reply Unknown.",
                ),
                "saved_food_edit_sugar": (
                    "_saved_food_edit_sugar_g",
                    "saved_food_edit_sodium",
                    "Enter the new milligrams of sodium, or reply Unknown.",
                ),
            }

            updated = dict(known_data)

            if current_step in field_steps:
                field, next_step, prompt = field_steps[current_step]
                updated[field] = number
                update_conversation(
                    chat_id=chat_id,
                    current_step=next_step,
                    known_data=updated,
                    missing_fields=[],
                )
                send_telegram_msg(prompt, chat_id=chat_id)
                return

            updated["_saved_food_edit_sodium_mg"] = number
            new_version = int(
                updated["_saved_food_edit_old_version"]
            ) + 1

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_edit_confirmation",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                "Save these nutrition changes?\n\n"
                f"Food: {updated['_saved_food_edit_name']}\n"
                f"Serving: {updated['_saved_food_edit_serving']}\n"
                f"New nutrition version: {new_version}\n\n"
                f"Calories: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_calories'], 'cal')}\n"
                f"Protein: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_protein_g'], 'g')}\n"
                f"Carbohydrates: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_carbohydrates_g'], 'g')}\n"
                f"Fat: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_fat_g'], 'g')}\n"
                f"Fiber: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_fiber_g'], 'g')}\n"
                f"Sugar: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_sugar_g'], 'g')}\n"
                f"Sodium: "
                f"{format_nutrition_edit_value(updated['_saved_food_edit_sodium_mg'], 'mg')}\n\n"
                "Previously logged entries and saved recipe calculations "
                "will not change.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_edit_confirmation":
            if lowered in {"2", "no", "back"}:
                if known_data.get("_food_finder_return"):
                    send_telegram_msg(
                        "No nutrition changes were saved.",
                        chat_id=chat_id,
                    )
                    show_food_library_manage(
                        chat_id=chat_id,
                        food_id=int(
                            known_data.get("food_finder_food_id") or 0
                        ),
                    )
                    return
                if known_data.get(
                    "_pantry_organizer_nutrition_return"
                ):
                    send_telegram_msg(
                        "No nutrition changes were saved.",
                        chat_id=chat_id,
                    )
                    show_pantry_organize_nutrition(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            known_data.get("pantry_organize_id") or 0
                        ),
                        page=int(known_data.get("pantry_page") or 0),
                    )
                    return
                food_id = int(
                    known_data.get("_saved_food_edit_id") or 0
                )
                food = next(
                    (
                        item for item in list_user_saved_foods()
                        if int(item["food_id"]) == food_id
                    ),
                    None,
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_menu",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No nutrition changes were saved.\n\n"
                    + format_saved_food_edit_menu(food or {}),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            try:
                version_values = {
                    "food_id": int(known_data["_saved_food_edit_id"]),
                    "calories": float(
                        known_data["_saved_food_edit_calories"]
                    ),
                    "protein_g": (
                        None
                        if known_data["_saved_food_edit_protein_g"] is None
                        else float(known_data["_saved_food_edit_protein_g"])
                    ),
                    "carbohydrates_g": (
                        None
                        if known_data[
                            "_saved_food_edit_carbohydrates_g"
                        ] is None
                        else float(
                            known_data[
                                "_saved_food_edit_carbohydrates_g"
                            ]
                        )
                    ),
                    "fat_g": (
                        None
                        if known_data["_saved_food_edit_fat_g"] is None
                        else float(known_data["_saved_food_edit_fat_g"])
                    ),
                    "fiber_g": (
                        None
                        if known_data["_saved_food_edit_fiber_g"] is None
                        else float(known_data["_saved_food_edit_fiber_g"])
                    ),
                    "sugar_g": (
                        None
                        if known_data["_saved_food_edit_sugar_g"] is None
                        else float(known_data["_saved_food_edit_sugar_g"])
                    ),
                    "sodium_mg": (
                        None
                        if known_data["_saved_food_edit_sodium_mg"] is None
                        else float(known_data["_saved_food_edit_sodium_mg"])
                    ),
                }
                if known_data.get("_food_finder_return"):
                    version_values["serving_description"] = str(
                        known_data["_saved_food_edit_serving"]
                    )
                version = add_user_nutrition_version(**version_values)
            except Exception:
                logging.exception("Could not edit saved food")
                send_telegram_msg(
                    "I could not save those changes. "
                    "The existing nutrition is still active.",
                    chat_id=chat_id,
                )
                return

            food_name = str(
                known_data["_saved_food_edit_name"]
            )
            if known_data.get("_food_finder_return"):
                send_telegram_msg(
                    f"Updated {food_name} to nutrition version "
                    f"{version['version_number']}.\n"
                    "Past logs and saved recipe calculations were not "
                    "changed.",
                    chat_id=chat_id,
                )
                show_food_library_manage(
                    chat_id=chat_id,
                    food_id=int(
                        known_data.get("food_finder_food_id") or 0
                    ),
                )
                return
            if known_data.get("_pantry_organizer_nutrition_return"):
                send_telegram_msg(
                    f"Updated {food_name} to nutrition version "
                    f"{version['version_number']}.\n"
                    "Previously logged entries and saved recipe "
                    "calculations were not changed.",
                    chat_id=chat_id,
                )
                show_pantry_organize_nutrition(
                    chat_id=chat_id,
                    pantry_item_id=int(
                        known_data.get("pantry_organize_id") or 0
                    ),
                    page=int(known_data.get("pantry_page") or 0),
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_details",
                known_data={
                    "_saved_food_edit_old_version": int(
                        version["version_number"]
                    ),
                },
                missing_fields=[],
            )
            updated_food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == int(
                        known_data["_saved_food_edit_id"]
                    )
                ),
                None,
            )
            send_telegram_msg(
                f"Updated {food_name} to nutrition version "
                f"{version['version_number']}.\n"
                "Previously logged entries were not changed.\n\n"
                + format_saved_food_details(updated_food or {}),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_add_name":
            name = text.strip()

            if len(name) < 2:
                send_telegram_msg(
                    "Please enter a food name with at least two characters.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_add_serving",
                known_data={"_saved_food_name": name},
                missing_fields=[],
            )
            send_telegram_msg(
                "What is one serving?\n\n"
                "Examples:\n"
                "- 1 bowl\n"
                "- 1 bar\n"
                "- 12 fl oz\n"
                "- 4 ounces",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_add_serving":
            if lowered == "back" and known_data.get(
                "_pantry_nutrition_return"
            ):
                show_pantry_nutrition_options(
                    chat_id=chat_id,
                    pantry_item_id=int(
                        known_data.get("pantry_nutrition_item_id") or 0
                    ),
                    known_data=known_data,
                )
                return
            serving = text.strip()

            if len(serving) < 2:
                send_telegram_msg(
                    "Please describe one serving, such as 1 bowl.",
                    chat_id=chat_id,
                )
                return

            updated = dict(known_data)
            updated["_saved_food_serving"] = serving
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_add_calories",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                "How many calories are in that serving?",
                chat_id=chat_id,
            )
            return

        if current_step in {
            "saved_food_add_calories",
            "saved_food_add_protein",
            "saved_food_add_carbohydrates",
            "saved_food_add_fat",
            "saved_food_add_fiber",
            "saved_food_add_sugar",
            "saved_food_add_sodium",
        }:
            if lowered == "back" and known_data.get(
                "_pantry_nutrition_return"
            ):
                show_pantry_nutrition_options(
                    chat_id=chat_id,
                    pantry_item_id=int(
                        known_data.get("pantry_nutrition_item_id") or 0
                    ),
                    known_data=known_data,
                )
                return
            cleaned_number = (
                text.strip().lower()
                .replace(",", "")
                .replace("calories", "")
                .replace("calorie", "")
                .replace("cals", "")
                .replace("cal", "")
                .replace("grams", "")
                .replace("gram", "")
                .replace("mg", "")
                .replace("g", "")
                .strip()
            )

            try:
                number = float(cleaned_number)
            except ValueError:
                send_telegram_msg(
                    "Please enter a non-negative number.",
                    chat_id=chat_id,
                )
                return

            if number < 0:
                send_telegram_msg(
                    "Nutrition values cannot be negative.",
                    chat_id=chat_id,
                )
                return

            field_steps = {
                "saved_food_add_calories": (
                    "_saved_food_calories",
                    "saved_food_add_protein",
                    "How many grams of protein?",
                ),
                "saved_food_add_protein": (
                    "_saved_food_protein_g",
                    "saved_food_add_carbohydrates",
                    "How many grams of carbohydrates?",
                ),
                "saved_food_add_carbohydrates": (
                    "_saved_food_carbohydrates_g",
                    "saved_food_add_fat",
                    "How many grams of fat?",
                ),
                "saved_food_add_fat": (
                    "_saved_food_fat_g",
                    "saved_food_add_fiber",
                    "How many grams of fiber?",
                ),
                "saved_food_add_fiber": (
                    "_saved_food_fiber_g",
                    "saved_food_add_sugar",
                    "How many grams of sugar?",
                ),
                "saved_food_add_sugar": (
                    "_saved_food_sugar_g",
                    "saved_food_add_sodium",
                    "How many milligrams of sodium?",
                ),
            }

            updated = dict(known_data)

            if current_step in field_steps:
                field, next_step, prompt = field_steps[current_step]
                updated[field] = number
                update_conversation(
                    chat_id=chat_id,
                    current_step=next_step,
                    known_data=updated,
                    missing_fields=[],
                )
                send_telegram_msg(prompt, chat_id=chat_id)
                return

            updated["_saved_food_sodium_mg"] = number
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_add_confirmation",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                "Save this saved food?\n\n"
                f"Food: {updated['_saved_food_name']}\n"
                f"Serving: {updated['_saved_food_serving']}\n"
                f"Calories: {format_display_number(updated['_saved_food_calories'])}\n"
                f"Protein: {format_display_number(updated['_saved_food_protein_g'])} g\n"
                f"Carbohydrates: {format_display_number(updated['_saved_food_carbohydrates_g'])} g\n"
                f"Fat: {format_display_number(updated['_saved_food_fat_g'])} g\n"
                f"Fiber: {format_display_number(updated['_saved_food_fiber_g'])} g\n"
                f"Sugar: {format_display_number(updated['_saved_food_sugar_g'])} g\n"
                f"Sodium: {format_display_number(updated['_saved_food_sodium_mg'])} mg\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_add_confirmation":
            if lowered in {"2", "no", "back"}:
                if known_data.get("_pantry_nutrition_return"):
                    send_telegram_msg(
                        "No Saved Food was added. Nothing was linked.",
                        chat_id=chat_id,
                    )
                    show_pantry_nutrition_options(
                        chat_id=chat_id,
                        pantry_item_id=int(
                            known_data.get("pantry_nutrition_item_id") or 0
                        ),
                        known_data=known_data,
                    )
                    return
                if known_data.get("_recipe_import_return"):
                    updated = dict(known_data)
                    updated["_recipe_builder_return"] = False
                    updated["_recipe_import_return"] = False
                    send_telegram_msg(
                        "No Saved Food was added.",
                        chat_id=chat_id,
                    )
                    continue_recipe_import_resolution(
                        chat_id=chat_id,
                        known_data=updated,
                    )
                    return
                if known_data.get("_recipe_builder_return"):
                    send_telegram_msg(
                        "No Saved Food was added.",
                        chat_id=chat_id,
                    )
                    update_conversation(
                        chat_id=chat_id,
                        known_data={"_recipe_builder_return": False},
                    )
                    show_recipe_builder_pantry_choices(chat_id=chat_id)
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No saved food was added.\n\n"
                    + healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            serving_description = str(
                known_data["_saved_food_serving"]
            ).strip()
            serving_match = re.fullmatch(
                r"\s*([0-9]+(?:\.[0-9]+)?)\s+(.+?)\s*",
                serving_description,
            )

            if serving_match:
                serving_amount = float(serving_match.group(1))
                serving_unit = serving_match.group(2)
            else:
                serving_amount = 1.0
                serving_unit = serving_description

            try:
                result = add_food_with_nutrition(
                    canonical_name=str(
                        known_data["_saved_food_name"]
                    ).strip(),
                    serving_description=serving_description,
                    serving_amount=serving_amount,
                    serving_unit=serving_unit,
                    verification_status="verified",
                    verification_source="user_entered",
                    calories=float(
                        known_data["_saved_food_calories"]
                    ),
                    protein_g=float(
                        known_data["_saved_food_protein_g"]
                    ),
                    carbohydrates_g=float(
                        known_data["_saved_food_carbohydrates_g"]
                    ),
                    fat_g=float(
                        known_data["_saved_food_fat_g"]
                    ),
                    fiber_g=float(
                        known_data["_saved_food_fiber_g"]
                    ),
                    sugar_g=float(
                        known_data["_saved_food_sugar_g"]
                    ),
                    sodium_mg=float(
                        known_data["_saved_food_sodium_mg"]
                    ),
                )
            except Exception:
                logging.exception("Could not add saved food")
                send_telegram_msg(
                    "I could not save that food. Nothing was changed.",
                    chat_id=chat_id,
                )
                return

            food = result["food"]
            created = bool(result.get("created"))
            if known_data.get("_pantry_nutrition_return"):
                try:
                    linked = link_pantry_item_to_food(
                        int(
                            known_data.get("pantry_nutrition_item_id") or 0
                        ),
                        food_id=int(food["food_id"]),
                        source="saved_food",
                    )
                except (TypeError, ValueError) as exc:
                    send_telegram_msg(
                        "The Saved Food was created, but I couldn't link it "
                        f"to the Pantry item: {exc}",
                        chat_id=chat_id,
                    )
                    return
                finish_pantry_nutrition_link(
                    chat_id=chat_id,
                    known_data=known_data,
                    pantry_item=linked,
                    food_name=str(food["canonical_name"]),
                )
                return
            if known_data.get("_recipe_import_return"):
                pending = list(
                    known_data.get("_recipe_import_pending") or []
                )
                if not pending:
                    send_telegram_msg(
                        "The imported ingredient expired. Please restart "
                        "Recipe Import.",
                        chat_id=chat_id,
                    )
                    return
                current = dict(pending[0])
                try:
                    ingredient = prepare_recipe_ingredient(
                        food_id=int(food["food_id"]),
                        amount_description=str(
                            current.get("amount_description") or ""
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    current["candidate_food_id"] = int(food["food_id"])
                    current["candidate_food_name"] = str(
                        food.get("canonical_name") or ""
                    )
                    current["candidate_serving_description"] = str(
                        food.get("serving_description") or "not listed"
                    )
                    current["conversion_error"] = str(exc)
                    updated = dict(known_data)
                    updated["_recipe_builder_return"] = False
                    updated["_recipe_import_return"] = False
                    updated["_recipe_import_pending"] = [
                        current,
                        *pending[1:],
                    ]
                    update_conversation(
                        chat_id=chat_id,
                        current_step="recipe_import_ingredient",
                        known_data=updated,
                        missing_fields=["recipe_ingredient_amount"],
                    )
                    send_telegram_msg(
                        "Saved the new ingredient, but the imported amount "
                        f"still needs clarification.\n\n{exc}\n\n"
                        + format_recipe_import_missing(current),
                        chat_id=chat_id,
                    )
                    return
                ingredients = list(
                    known_data.get("_recipe_builder_ingredients") or []
                )
                ingredients.append(
                    annotate_imported_recipe_ingredient(ingredient, current)
                )
                created_ids = list(
                    known_data.get("_recipe_import_created_food_ids") or []
                )
                if created:
                    created_ids.append(int(food["food_id"]))
                updated = dict(known_data)
                updated["_recipe_builder_return"] = False
                updated["_recipe_import_return"] = False
                updated["_recipe_builder_ingredients"] = ingredients
                updated["_recipe_import_pending"] = pending[1:]
                updated["_recipe_import_created_food_ids"] = created_ids
                continue_recipe_import_resolution(
                    chat_id=chat_id,
                    known_data=updated,
                )
                return
            if known_data.get("_recipe_builder_return"):
                add_pantry_item(
                    display_name=str(food.get("canonical_name") or "").strip(),
                    food_id=int(food["food_id"]),
                    source="saved_food",
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="recipe_builder_ingredient_amount",
                    known_data={
                        "_recipe_builder_return": False,
                        "_recipe_builder_selected_food_id": int(
                            food["food_id"]
                        ),
                    },
                    missing_fields=["ingredient_amount"],
                )
                saved_note = (
                    "Saved the new ingredient."
                    if created
                    else "That Saved Food already existed."
                )
                send_telegram_msg(
                    f"{saved_note}\n\n"
                    f"How much {food.get('canonical_name')} is in the "
                    "entire recipe?\n\n"
                    f"Saved serving: {food.get('serving_description')}\n"
                    "Examples: 1 serving, 40 g, 3 oz, or 2 slices.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_foods",
                known_data={},
                missing_fields=[],
            )

            if created:
                result_message = (
                    f"Saved {food['canonical_name']} "
                    "to your food library."
                )
            else:
                result_message = (
                    f"{food['canonical_name']} already exists. "
                    "No duplicate was created."
                )

            send_telegram_msg(
                result_message
                + "\n\n"
                + healthcoach_saved_foods_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_browse":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            food_ids = list(
                known_data.get("_saved_food_ids") or []
            )

            try:
                selection = int(lowered)
            except ValueError:
                selection = 0

            if selection < 1 or selection > len(food_ids):
                send_telegram_msg(
                    "Choose one of the numbered saved foods, "
                    "or reply Back.",
                    chat_id=chat_id,
                )
                return

            selected_id = int(food_ids[selection - 1])
            foods = list_user_saved_foods()
            selected = next(
                (
                    food for food in foods
                    if int(food["food_id"]) == selected_id
                ),
                None,
            )

            if selected is None:
                send_telegram_msg(
                    "That saved food is no longer available.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_details",
                known_data={
                    "_saved_food_edit_id": selected_id,
                    "_saved_food_edit_name": selected["canonical_name"],
                    "_saved_food_edit_serving": (
                        selected.get("serving_description")
                        or "1 serving"
                    ),
                    "_saved_food_edit_old_version": int(
                        selected.get("version_number") or 1
                    ),
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_saved_food_details(selected),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_details":
            food_id = int(
                known_data.get("_saved_food_edit_id") or 0
            )
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )
            if food is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That Saved Food is no longer available.\n\n"
                    + healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered == "back":
                foods = list_user_saved_foods()
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_browse",
                    known_data={
                        "_saved_food_ids": [
                            int(item["food_id"])
                            for item in foods
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_choices(foods),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "edit",
                "edit saved food",
                "edit entered food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_menu",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_edit_menu(food),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "delete",
                "delete saved food",
                "remove entered food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_delete_confirmation",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Remove this Saved Food?\n\n"
                    f"Food: {food.get('canonical_name')}\n"
                    "Serving: "
                    f"{food.get('serving_description') or '1 serving'}\n\n"
                    "It will disappear from Saved Foods. Previously "
                    "logged entries will remain unchanged.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_saved_food_details(food),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_delete_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            food_ids = list(
                known_data.get("_saved_food_ids") or []
            )
            try:
                selection = int(lowered)
            except ValueError:
                selection = 0
            if selection < 1 or selection > len(food_ids):
                send_telegram_msg(
                    "Choose one of the numbered saved foods, or reply Back.",
                    chat_id=chat_id,
                )
                return
            food_id = int(food_ids[selection - 1])
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )
            if food is None:
                send_telegram_msg(
                    "That Saved Food is no longer available.",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_delete_confirmation",
                known_data={
                    "_saved_food_edit_id": food_id,
                    "_saved_food_edit_name": food["canonical_name"],
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Remove this Saved Food?\n\n"
                f"Food: {food.get('canonical_name')}\n"
                "Serving: "
                f"{food.get('serving_description') or '1 serving'}\n\n"
                "It will disappear from Saved Foods. Previously logged "
                "entries will remain unchanged.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_delete_confirmation":
            food_id = int(
                known_data.get("_saved_food_edit_id") or 0
            )
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )
            if food is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That Saved Food is already gone.\n\n"
                    + healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_details",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_details(food),
                    chat_id=chat_id,
                )
                return
            if lowered not in {"1", "yes", "remove", "delete"}:
                send_telegram_msg(
                    "Reply Yes, No, Back, or Cancel.",
                    chat_id=chat_id,
                )
                return
            try:
                archived = archive_user_saved_food(food_id)
            except (RuntimeError, ValueError) as exc:
                send_telegram_msg(
                    f"I couldn't remove that Saved Food: {exc}",
                    chat_id=chat_id,
                )
                return
            update_conversation(
                chat_id=chat_id,
                current_step="saved_foods",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                f"Removed {archived.get('canonical_name')} from Saved "
                "Foods. Previously logged entries were not changed.\n\n"
                + healthcoach_saved_foods_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "restaurant":
            if lowered in {
                "1",
                "find best choices online",
                "find choices",
                "search",
                "search again",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="restaurant_query",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What restaurant are you at?\n\n"
                    "Include the city and state for a local restaurant.\n"
                    "Example: Red Robin in Redding, California",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"2", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_restaurant_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "restaurant_query":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="restaurant",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_restaurant_menu_text(),
                    chat_id=chat_id,
                )
                return

            if len(text.strip()) < 2:
                send_telegram_msg(
                    "Please send the restaurant name. Include the city "
                    "and state if it is a local restaurant.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                "I'm checking the current menu and cited nutrition. "
                "This may take a moment.",
                chat_id=chat_id,
                remove_keyboard=True,
            )

            try:
                advice = recommend_restaurant_entrees(text)
            except Exception:
                logging.exception(
                    "Restaurant recommendation lookup failed"
                )
                advice = {
                    "found": False,
                    "restaurant_display_name": text,
                    "candidates": [],
                    "notes": [
                        "The online menu lookup failed. Please try again."
                    ],
                }

            update_conversation(
                chat_id=chat_id,
                current_step="restaurant",
                known_data={
                    "_restaurant_query": text,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_restaurant_advice(advice),
                chat_id=chat_id,
            )
            return

        if current_step == "favorites":
            if lowered in {"1", "quick log", "quick"}:
                favorites = list_food_favorites()
                if not favorites:
                    send_telegram_msg(
                        "You do not have any saved favorites yet.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_quick_select",
                    known_data={
                        "_favorite_ids": [
                            int(item["food_favorite_id"])
                            for item in favorites
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_favorite_choices(
                        favorites,
                        action="log",
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "save today's food", "save today"}:
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda item: int(item["food_entry_id"]),
                    reverse=True,
                )
                if not entries:
                    send_telegram_msg(
                        "There are no foods logged today to save.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_save_select",
                    known_data={
                        "_entry_ids": [
                            int(item["food_entry_id"])
                            for item in entries
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_save_favorite_choices(entries),
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "remove favorite", "remove"}:
                favorites = list_food_favorites()
                if not favorites:
                    send_telegram_msg(
                        "You do not have any saved favorites yet.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_remove_select",
                    known_data={
                        "_favorite_ids": [
                            int(item["food_favorite_id"])
                            for item in favorites
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_favorite_choices(
                        favorites,
                        action="remove",
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_favorites_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "favorite_quick_select",
            "favorite_save_select",
            "favorite_remove_select",
        }:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorites",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_favorites_menu_text(),
                    chat_id=chat_id,
                )
                return

            try:
                choice = int(lowered)
            except ValueError:
                choice = 0

            if current_step == "favorite_save_select":
                ids = list(known_data.get("_entry_ids") or [])
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda item: int(item["food_entry_id"]),
                    reverse=True,
                )
                if choice < 1 or choice > len(ids):
                    send_telegram_msg(
                        format_save_favorite_choices(entries),
                        chat_id=chat_id,
                    )
                    return
                entry_id = int(ids[choice - 1])
                entry = next(
                    (
                        item for item in entries
                        if int(item["food_entry_id"]) == entry_id
                    ),
                    None,
                )
                if entry is None:
                    send_telegram_msg(
                        "That food entry no longer exists.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_save_confirmation",
                    known_data={"_entry_id": entry_id},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Save this favorite?\n\n"
                    f"Food: {entry['canonical_name']}\n"
                    f"Meal: {str(entry['meal_category']).title()}\n"
                    f"Quantity: {format_display_number(float(entry['quantity']))}\n\n"
                    "1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            favorites = list_food_favorites()
            ids = list(known_data.get("_favorite_ids") or [])
            if choice < 1 or choice > len(ids):
                send_telegram_msg(
                    format_favorite_choices(
                        favorites,
                        action=(
                            "remove"
                            if current_step == "favorite_remove_select"
                            else "log"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return
            favorite_id = int(ids[choice - 1])
            favorite = next(
                (
                    item for item in favorites
                    if int(item["food_favorite_id"])
                    == favorite_id
                ),
                None,
            )
            if favorite is None:
                send_telegram_msg(
                    "That favorite no longer exists.",
                    chat_id=chat_id,
                )
                return

            is_remove = current_step == "favorite_remove_select"
            update_conversation(
                chat_id=chat_id,
                current_step=(
                    "favorite_remove_confirmation"
                    if is_remove
                    else "favorite_quick_confirmation"
                ),
                known_data={"_favorite_id": favorite_id},
                missing_fields=[],
            )
            send_telegram_msg(
                (
                    "Remove this favorite?\n\n"
                    if is_remove
                    else "Quick-log this favorite?\n\n"
                )
                + f"Food: {favorite['canonical_name']}\n"
                + f"Meal: {str(favorite['meal_category']).title()}\n"
                + f"Quantity: {format_display_number(float(favorite['quantity']))}\n\n"
                + "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step in {
            "favorite_save_confirmation",
            "favorite_quick_confirmation",
            "favorite_remove_confirmation",
        }:
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorites",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No changes were made.\n\n"
                    + healthcoach_favorites_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes"}:
                prompt = {
                    "favorite_save_confirmation": "Save this favorite?",
                    "favorite_quick_confirmation": "Quick-log this favorite?",
                    "favorite_remove_confirmation": "Remove this favorite?",
                }[current_step]
                send_telegram_msg(
                    prompt + "\n\n1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            if current_step == "favorite_save_confirmation":
                try:
                    favorite = save_food_favorite_from_entry(
                        int(known_data["_entry_id"])
                    )
                except (KeyError, ValueError) as error:
                    send_telegram_msg(str(error), chat_id=chat_id)
                    return
                result_message = (
                    f"Saved {favorite['canonical_name']} as a favorite."
                )

            elif current_step == "favorite_remove_confirmation":
                removed = delete_food_favorite(
                    int(known_data.get("_favorite_id") or 0)
                )
                result_message = (
                    "Favorite removed."
                    if removed
                    else "That favorite no longer exists."
                )

            else:
                favorite_id = int(
                    known_data.get("_favorite_id") or 0
                )
                favorite = next(
                    (
                        item for item in list_food_favorites()
                        if int(item["food_favorite_id"])
                        == favorite_id
                    ),
                    None,
                )
                if favorite is None:
                    send_telegram_msg(
                        "That favorite no longer exists.",
                        chat_id=chat_id,
                    )
                    return
                duplicate = find_recent_duplicate_entry(
                    entry_date=today,
                    meal_category=favorite["meal_category"],
                    food_id=int(favorite["food_id"]),
                    quantity=float(favorite["quantity"]),
                    window_minutes=5,
                )
                if duplicate is not None:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="favorites",
                        known_data={},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        "That exact favorite was logged within the "
                        "last five minutes, so it was not logged again.\n\n"
                        + healthcoach_favorites_menu_text(),
                        chat_id=chat_id,
                    )
                    return
                try:
                    add_food_entry(
                        entry_date=today,
                        meal_category=favorite["meal_category"],
                        food_id=int(favorite["food_id"]),
                        quantity=float(favorite["quantity"]),
                        logging_source="telegram_ai",
                        original_text="Quick logged from favorites",
                        quantity_is_estimated=False,
                        user_confirmed=True,
                    )
                except ValueError as error:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="favorites",
                        known_data={},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        str(error) + "\n\n"
                        + healthcoach_favorites_menu_text(),
                        chat_id=chat_id,
                    )
                    return
                except Exception:
                    logging.exception("Favorite quick log failed")
                    send_telegram_msg(
                        "The favorite could not be logged.",
                        chat_id=chat_id,
                    )
                    return
                try:
                    sync_food_ledger_totals_to_sheet(today)
                except Exception:
                    logging.exception(
                        "Favorite Google Sheet sync failed"
                    )
                    result_message = (
                        f"Quick logged {favorite['canonical_name']}, "
                        "but the Google Sheet totals could not be updated."
                    )
                else:
                    result_message = (
                        f"Quick logged {favorite['canonical_name']} for "
                        f"{str(favorite['meal_category']).title()}."
                    )

            update_conversation(
                chat_id=chat_id,
                current_step="favorites",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                append_goal_calorie_progress(result_message, today)
                + "\n\n"
                + healthcoach_favorites_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            entry_ids = list(
                known_data.get("_edit_food_entry_ids") or []
            )

            try:
                choice = int(lowered)
            except ValueError:
                choice = 0

            if choice < 1 or choice > len(entry_ids):
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda entry: int(entry["food_entry_id"]),
                    reverse=True,
                )
                send_telegram_msg(
                    format_edit_food_choices(entries),
                    chat_id=chat_id,
                )
                return

            entry_id = int(entry_ids[choice - 1])
            entry = next(
                (
                    candidate
                    for candidate in list_food_entries(
                        entry_date=today
                    )
                    if int(candidate["food_entry_id"]) == entry_id
                ),
                None,
            )

            if entry is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That food entry no longer exists.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="edit_food_action",
                known_data={"_edit_food_entry_id": entry_id},
                missing_fields=[],
            )
            send_telegram_msg(
                format_edit_food_action(entry),
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_action":
            entry_id = known_data.get("_edit_food_entry_id")
            entry = next(
                (
                    candidate
                    for candidate in list_food_entries(
                        entry_date=today
                    )
                    if int(candidate["food_entry_id"])
                    == int(entry_id)
                ),
                None,
            ) if entry_id is not None else None

            if entry is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That food entry no longer exists.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"1", "quantity"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_quantity",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Send the new numeric quantity.\n\n"
                    "Examples: 0.5, 1, or 1.3 servings.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"2", "meal"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_meal",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Choose the new meal:\n\n"
                    "Before breakfast\nBreakfast\nSchool snack\n"
                    "Lunch\nAfternoon snack\nDinner\nDessert",
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "back"}:
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda candidate: int(
                        candidate["food_entry_id"]
                    ),
                    reverse=True,
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_select",
                    known_data={
                        "_edit_food_entry_ids": [
                            int(candidate["food_entry_id"])
                            for candidate in entries
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_edit_food_choices(entries),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_edit_food_action(entry),
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_quantity":
            match = re.fullmatch(
                r"(?:quantity\s+)?"
                r"((?:\d+(?:\.\d+)?)|(?:\.\d+))"
                r"(?:\s+servings?)?",
                lowered,
            )

            if match is None or float(match.group(1)) <= 0:
                send_telegram_msg(
                    "Please send a quantity greater than zero.\n"
                    "Examples: 0.5, 1, or 1.3 servings.",
                    chat_id=chat_id,
                )
                return

            new_quantity = float(match.group(1))
            update_conversation(
                chat_id=chat_id,
                current_step="edit_food_confirmation",
                known_data={
                    **known_data,
                    "_edit_field": "quantity",
                    "_edit_value": new_quantity,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Apply this food change?\n\n"
                f"New quantity: {format_display_number(new_quantity)}\n\n"
                "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_meal":
            meal_aliases = {
                "before breakfast": "before breakfast",
                "breakfast": "breakfast",
                "school snack": "school snack",
                "morning snack": "school snack",
                "lunch": "lunch",
                "afternoon snack": "afternoon snack",
                "snack": "afternoon snack",
                "dinner": "dinner",
                "dessert": "dessert",
            }

            if lowered == "back":
                entry_id = known_data.get("_edit_food_entry_id")
                entry = next(
                    (
                        candidate
                        for candidate in list_food_entries(
                            entry_date=today
                        )
                        if int(candidate["food_entry_id"])
                        == int(entry_id)
                    ),
                    None,
                ) if entry_id is not None else None
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_action",
                    known_data=known_data,
                    missing_fields=[],
                )
                if entry is not None:
                    send_telegram_msg(
                        format_edit_food_action(entry),
                        chat_id=chat_id,
                    )
                return

            new_meal = meal_aliases.get(lowered)
            if new_meal is None:
                send_telegram_msg(
                    "Choose the new meal:\n\n"
                    "Before breakfast\nBreakfast\nSchool snack\n"
                    "Lunch\nAfternoon snack\nDinner\nDessert",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="edit_food_confirmation",
                known_data={
                    **known_data,
                    "_edit_field": "meal",
                    "_edit_value": new_meal,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Apply this food change?\n\n"
                f"New meal: {new_meal.title()}\n\n"
                "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_confirmation":
            if lowered in {"2", "no", "back"}:
                entry_id = known_data.get("_edit_food_entry_id")
                entry = next(
                    (
                        candidate
                        for candidate in list_food_entries(
                            entry_date=today
                        )
                        if int(candidate["food_entry_id"])
                        == int(entry_id)
                    ),
                    None,
                ) if entry_id is not None else None
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_action",
                    known_data={"_edit_food_entry_id": entry_id},
                    missing_fields=[],
                )
                if entry is not None:
                    send_telegram_msg(
                        "No changes were made.\n\n"
                        + format_edit_food_action(entry),
                        chat_id=chat_id,
                    )
                return

            if lowered not in {"1", "yes"}:
                send_telegram_msg(
                    "Apply this food change?\n\n1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            entry_id = known_data.get("_edit_food_entry_id")
            edit_field = known_data.get("_edit_field")
            edit_value = known_data.get("_edit_value")

            try:
                if entry_id is None:
                    updated = None
                elif edit_field == "quantity":
                    updated = update_food_entry(
                        int(entry_id),
                        quantity=float(edit_value),
                    )
                elif edit_field == "meal":
                    updated = update_food_entry(
                        int(entry_id),
                        meal_category=str(edit_value),
                    )
                else:
                    updated = None
            except ValueError as error:
                send_telegram_msg(str(error), chat_id=chat_id)
                return

            if updated is None:
                send_telegram_msg(
                    "That food entry could not be updated.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(today)
            except Exception:
                logging.exception(
                    "Google Sheet sync failed after Food edit"
                )
                result_message = (
                    "Food was updated, but the Google Sheet "
                    "could not be updated."
                )
            else:
                result_message = (
                    "Food updated and today's totals recalculated."
                )

            update_conversation(
                chat_id=chat_id,
                current_step="food",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                result_message + "\n\n" + healthcoach_food_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "undo_food_confirmation":
            if lowered in {"1", "yes", "undo"}:
                entry_id = known_data.get("_undo_food_entry_id")
                deleted = (
                    delete_food_entry(int(entry_id))
                    if entry_id is not None
                    else False
                )

                if not deleted:
                    send_telegram_msg(
                        "That food entry no longer exists.",
                        chat_id=chat_id,
                    )
                else:
                    try:
                        sync_food_ledger_totals_to_sheet(today)
                    except Exception:
                        logging.exception(
                            "Google Sheet sync failed after Food undo"
                        )
                        send_telegram_msg(
                            "Food was removed, but the Google Sheet "
                            "could not be updated.",
                            chat_id=chat_id,
                        )
                    else:
                        send_telegram_msg(
                            "The last food entry was removed and "
                            "today's totals were updated.",
                            chat_id=chat_id,
                        )

                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Food entry kept.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                "Please choose:\n"
                "1. Yes, undo it\n"
                "2. No, keep it",
                chat_id=chat_id,
            )
            return

        if current_step == "health":
            if lowered in {"1", "status", "current status"}:
                metrics = get_today_metrics()
                send_telegram_msg(
                    build_progress_message(
                        "Current status",
                        metrics,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "sleep", "record sleep"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_sleep_entry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "How much did you sleep?\n\n"
                    "Examples:\n"
                    "- 7:15\n"
                    "- I slept 7 hours\n"
                    "- I got 6 and a half hours\n"
                    "- I slept from 10:30 PM to 5:45 AM",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "weight", "record weight"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_weight_entry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What was your morning weight?\n\n"
                    "Examples: 214.6 or I weighed 214.6 this morning.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "4",
                "history",
                "health history",
                "weight history",
                "sleep history",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_history",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_health_history_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "5",
                "daily health insight",
                "daily insight",
                "health insight",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_daily_insight",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "I'm comparing your recorded health data with your "
                    "personal recent patterns. This may take a moment.",
                    chat_id=chat_id,
                )
                try:
                    message = get_daily_health_insight_message(
                        reference_date=today,
                    )
                except Exception:
                    logging.exception("Daily Health Insight failed")
                    send_telegram_msg(
                        "I couldn't build a reliable Daily Health Insight "
                        "right now.\n\n"
                        "No health data was changed. Reply Refresh insight, "
                        "Back, or Cancel.",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(message, chat_id=chat_id)
                return

            if lowered in {
                "6",
                "weekly health insight",
                "weekly insight",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_weekly_insight",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "I'm comparing your last completed week with the "
                    "week before it. This may take a moment.",
                    chat_id=chat_id,
                )
                try:
                    message = get_weekly_health_insight_message(
                        reference_date=today,
                    )
                except Exception:
                    logging.exception("Weekly Health Insight failed")
                    send_telegram_msg(
                        "I couldn't build a reliable Weekly Health Insight "
                        "right now.\n\nNo health data was changed. Reply "
                        "Refresh weekly insight, Back, or Cancel.",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(message, chat_id=chat_id)
                return

            if lowered in {"7", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="main",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_main_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_health_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "health_daily_insight":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="health",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_health_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "refresh",
                "refresh insight",
                "daily health insight",
                "daily insight",
            }:
                send_telegram_msg(
                    "I'm refreshing the comparison with your latest "
                    "recorded health data. This may take a moment.",
                    chat_id=chat_id,
                )
                try:
                    message = get_daily_health_insight_message(
                        reference_date=today,
                    )
                except Exception:
                    logging.exception("Daily Health Insight refresh failed")
                    send_telegram_msg(
                        "I couldn't refresh a reliable Daily Health Insight "
                        "right now.\n\n"
                        "No health data was changed. Reply Refresh insight, "
                        "Back, or Cancel.",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(message, chat_id=chat_id)
                return
            send_telegram_msg(
                "Reply Refresh insight, Back, or Cancel.",
                chat_id=chat_id,
            )
            return

        if current_step == "health_weekly_insight":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="health",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_health_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered in {
                "refresh",
                "refresh weekly insight",
                "weekly health insight",
                "weekly insight",
            }:
                send_telegram_msg(
                    "I'm refreshing the completed-week comparison. This "
                    "may take a moment.",
                    chat_id=chat_id,
                )
                try:
                    message = get_weekly_health_insight_message(
                        reference_date=today,
                    )
                except Exception:
                    logging.exception(
                        "Weekly Health Insight refresh failed"
                    )
                    send_telegram_msg(
                        "I couldn't refresh a reliable Weekly Health "
                        "Insight right now.\n\nNo health data was changed. "
                        "Reply Refresh weekly insight, Back, or Cancel.",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(message, chat_id=chat_id)
                return
            send_telegram_msg(
                "Reply Refresh weekly insight, Back, or Cancel.",
                chat_id=chat_id,
            )
            return

        if current_step == "health_history":
            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_health_menu_text(),
                    chat_id=chat_id,
                )
                return

            period_days = (
                7
                if lowered in {"1", "7", "7 days", "last 7 days"}
                else 14
                if lowered in {
                    "2",
                    "14",
                    "14 days",
                    "last 14 days",
                }
                else 30
                if lowered in {
                    "3",
                    "30",
                    "30 days",
                    "last 30 days",
                }
                else None
            )
            if period_days is None:
                send_telegram_msg(
                    healthcoach_health_history_menu_text(),
                    chat_id=chat_id,
                )
                return

            try:
                message = get_formatted_health_history(
                    reference_date=today,
                    days=period_days,
                )
            except Exception:
                logging.exception("Health history lookup failed")
                send_telegram_msg(
                    "I couldn't load Health History right now. "
                    "No health data was changed.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                message,
                chat_id=chat_id,
            )
            return

        if current_step == "heart_health_report":
            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="reports",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_reports_menu_text(),
                    chat_id=chat_id,
                )
                return

            period_days = (
                7
                if lowered in {"1", "7", "7 days", "last 7 days"}
                else 14
                if lowered in {
                    "2",
                    "14",
                    "14 days",
                    "last 14 days",
                }
                else 30
                if lowered in {
                    "3",
                    "30",
                    "30 days",
                    "last 30 days",
                }
                else None
            )
            if period_days is None:
                send_telegram_msg(
                    healthcoach_heart_health_menu_text(),
                    chat_id=chat_id,
                )
                return

            try:
                message = get_formatted_heart_health_report(
                    reference_date=today,
                    days=period_days,
                )
            except Exception:
                logging.exception("Heart Health Report lookup failed")
                send_telegram_msg(
                    "I couldn't load the Heart Health Report right "
                    "now. No health data was changed.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(message, chat_id=chat_id)
            return

        if current_step == "health_sleep_entry":
            sleep_value = extract_sleep_value_from_text(
                text,
                allow_bare=True,
            )

            if sleep_value is None:
                send_telegram_msg(
                    "I couldn't understand that sleep amount. "
                    "Try 7:15, 7 hours, or 6 and a half hours.",
                    chat_id=chat_id,
                )
                return

            success, response = set_today_sleep(sleep_value)
            update_conversation(
                chat_id=chat_id,
                current_step="health",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                response + "\n\n" + healthcoach_health_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "health_weight_entry":
            weight_value = extract_weight_value_from_text(
                text,
                allow_bare=True,
            )

            if weight_value is None:
                send_telegram_msg(
                    "I couldn't understand that weight. "
                    "Enter pounds, such as 214.6.",
                    chat_id=chat_id,
                )
                return

            success, response = set_today_weight(weight_value)
            update_conversation(
                chat_id=chat_id,
                current_step="health",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                response + "\n\n" + healthcoach_health_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "goals":
            if lowered in {"1", "view", "view active goal"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="goal_view",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_weight_goal(get_active_weight_goal()),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "add", "add weight goal"}:
                if get_active_weight_goal() is not None:
                    send_telegram_msg(
                        "An active weight goal already exists. Edit or "
                        "remove it before adding another.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="goal_add_weight",
                    known_data={},
                    missing_fields=["target_weight"],
                )
                send_telegram_msg(
                    "What is your goal weight in pounds?",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "update", "update goal"}:
                try:
                    message = update_and_format_weight_goal(
                        reference_date=today
                    )
                except (ValueError, RuntimeError) as error:
                    send_telegram_msg(str(error), chat_id=chat_id)
                    return
                except Exception:
                    logging.exception("Weight Goal update failed")
                    send_telegram_msg(
                        "I couldn't update the weight goal right now. "
                        "The previous calorie target was kept.",
                        chat_id=chat_id,
                    )
                    return
                send_telegram_msg(message, chat_id=chat_id)
                return

            if lowered in {"4", "edit", "edit goal"}:
                goal = get_active_weight_goal()
                if goal is None:
                    send_telegram_msg(
                        "No active weight goal exists. Add one first.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="goal_edit_weight",
                    known_data={},
                    missing_fields=["target_weight"],
                )
                send_telegram_msg(
                    "What should the new goal weight be?\n\n"
                    f"Current goal: {format_display_number(goal['target_weight'])} lb",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"5", "remove", "remove goal"}:
                goal = get_active_weight_goal()
                if goal is None:
                    send_telegram_msg(
                        "No active weight goal exists.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="goal_remove_confirmation",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Remove this active weight goal?\n\n"
                    f"Goal: {format_display_number(goal['target_weight'])} lb\n\n"
                    "Its history will be retained.\n\n"
                    "1. Yes\n"
                    "2. No",
                    chat_id=chat_id,
                )
                return

            if lowered in {"6", "history", "goal history"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="goal_history",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_weight_goal_history(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"7", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="reports",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_reports_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_goals_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step in {"goal_view", "goal_history"}:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="goals",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_goals_menu_text(),
                    chat_id=chat_id,
                )
                return

            message = (
                format_weight_goal(get_active_weight_goal())
                if current_step == "goal_view"
                else format_weight_goal_history()
            )
            send_telegram_msg(message, chat_id=chat_id)
            return

        if current_step in {"goal_add_weight", "goal_edit_weight"}:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="goals",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_goals_menu_text(),
                    chat_id=chat_id,
                )
                return
            target_weight = parse_weight_goal_weight(text)
            if target_weight is None:
                send_telegram_msg(
                    "Enter a goal weight in pounds, such as 215.",
                    chat_id=chat_id,
                )
                return
            next_step = (
                "goal_add_date"
                if current_step == "goal_add_weight"
                else "goal_edit_date"
            )
            update_conversation(
                chat_id=chat_id,
                current_step=next_step,
                known_data={"goal_target_weight": target_weight},
                missing_fields=["target_date"],
            )
            send_telegram_msg(
                "What is the goal date?\n\n"
                "Example: 10/17/2026 or October 17, 2026",
                chat_id=chat_id,
            )
            return

        if current_step in {"goal_add_date", "goal_edit_date"}:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="goals",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_goals_menu_text(),
                    chat_id=chat_id,
                )
                return
            target_date = parse_weight_goal_date(
                text,
                reference_date=today,
            )
            if target_date is None or target_date <= today:
                send_telegram_msg(
                    "Enter a future goal date, such as 10/17/2026.",
                    chat_id=chat_id,
                )
                return
            target_weight = float(known_data["goal_target_weight"])

            if current_step == "goal_add_date":
                try:
                    inputs = get_weight_goal_health_inputs(
                        reference_date=today
                    )
                except Exception:
                    logging.exception("Goal starting weight lookup failed")
                    send_telegram_msg(
                        "I couldn't read your official weight right now. "
                        "Nothing was saved.",
                        chat_id=chat_id,
                    )
                    return
                if inputs["current_weight"] is None:
                    send_telegram_msg(
                        "Record an official morning weight before adding "
                        "a weight goal.",
                        chat_id=chat_id,
                    )
                    return
                start_weight = float(inputs["current_weight"])
                start_date = inputs["weight_date"] or today
                if target_weight >= start_weight:
                    send_telegram_msg(
                        "For this weight-loss goal, the goal weight must be "
                        "below your current official weight.",
                        chat_id=chat_id,
                    )
                    return
                next_step = "goal_add_confirmation"
                prompt = (
                    "Save this weight goal?\n\n"
                    f"Starting weight: {format_display_number(start_weight)} lb\n"
                    f"Goal weight: {format_display_number(target_weight)} lb\n"
                    f"Goal date: {target_date.strftime('%b')} {target_date.day}, "
                    f"{target_date.year}\n\n"
                    "No calorie target will be calculated until you choose "
                    "Update goal.\n\n1. Yes\n2. No"
                )
                extra = {
                    "goal_start_weight": start_weight,
                    "goal_start_date": start_date.isoformat(),
                }
            else:
                next_step = "goal_edit_confirmation"
                prompt = (
                    "Save these goal changes?\n\n"
                    f"New goal weight: {format_display_number(target_weight)} lb\n"
                    f"New goal date: {target_date.strftime('%b')} "
                    f"{target_date.day}, {target_date.year}\n\n"
                    "The saved calorie target will remain unchanged until "
                    "you choose Update goal.\n\n1. Yes\n2. No"
                )
                extra = {}

            update_conversation(
                chat_id=chat_id,
                current_step=next_step,
                known_data={
                    "goal_target_date": target_date.isoformat(),
                    **extra,
                },
                missing_fields=[],
            )
            send_telegram_msg(prompt, chat_id=chat_id)
            return

        if current_step == "goal_add_confirmation":
            if lowered in {"1", "yes", "save"}:
                try:
                    create_weight_goal(
                        start_date=known_data["goal_start_date"],
                        start_weight=float(known_data["goal_start_weight"]),
                        target_weight=float(known_data["goal_target_weight"]),
                        target_date=known_data["goal_target_date"],
                    )
                except ValueError as error:
                    send_telegram_msg(str(error), chat_id=chat_id)
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="goals",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Weight goal saved. Choose Update goal when you want "
                    "the calorie target calculated.\n\n"
                    + healthcoach_goals_menu_text(),
                    chat_id=chat_id,
                )
                return
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="goals",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No goal was added.\n\n" + healthcoach_goals_menu_text(),
                    chat_id=chat_id,
                )
                return
            send_telegram_msg("Please reply Yes or No.", chat_id=chat_id)
            return

        if current_step == "goal_edit_confirmation":
            if lowered in {"1", "yes", "save"}:
                try:
                    update_active_weight_goal(
                        target_weight=float(known_data["goal_target_weight"]),
                        target_date=known_data["goal_target_date"],
                    )
                except ValueError as error:
                    send_telegram_msg(str(error), chat_id=chat_id)
                    return
                result = (
                    "Weight goal changed. Its saved calorie target was not "
                    "recalculated. Choose Update goal when ready."
                )
            elif lowered in {"2", "no", "back"}:
                result = "The weight goal was not changed."
            else:
                send_telegram_msg("Please reply Yes or No.", chat_id=chat_id)
                return
            update_conversation(
                chat_id=chat_id,
                current_step="goals",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                result + "\n\n" + healthcoach_goals_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "goal_remove_confirmation":
            if lowered in {"1", "yes", "remove"}:
                try:
                    archive_active_weight_goal()
                except ValueError as error:
                    send_telegram_msg(str(error), chat_id=chat_id)
                    return
                result = "The active weight goal was removed. Its history was kept."
            elif lowered in {"2", "no", "back"}:
                result = "The active weight goal was kept."
            else:
                send_telegram_msg("Please reply Yes or No.", chat_id=chat_id)
                return
            update_conversation(
                chat_id=chat_id,
                current_step="goals",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                result + "\n\n" + healthcoach_goals_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "reports":
            if lowered in {"1", "today", "today's summary"}:
                metrics = get_today_metrics()
                send_telegram_msg(
                    build_progress_message(
                        "Today's summary",
                        metrics,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "weekly", "weekly report"}:
                cancel_conversation(chat_id)
                send_weekly_report(
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "goals", "goal"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="goals",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_goals_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "4",
                "heart health",
                "heart health report",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="heart_health_report",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_heart_health_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"5", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="main",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_main_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_reports_menu_text(),
                chat_id=chat_id,
            )
            return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "unresolved_food_review"
        and active_conversation.get("current_step")
        == "edit_details"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        unresolved_food_id = known_data.get(
            "_unresolved_food_id"
        )
        skipped_ids = list(
            known_data.get("_skipped_unresolved_food_ids") or []
        )

        try:
            interpretation = interpret_food_message(text)
        except Exception:
            logging.exception(
                "Unresolved Food detail interpretation failed"
            )
            send_telegram_msg(
                "I could not interpret those details. Try again.",
                chat_id=chat_id,
            )
            return

        if (
            unresolved_food_id is None
            or not interpretation.is_food_logging_request
            or not interpretation.food_name
        ):
            send_telegram_msg(
                "Please send a complete corrected food description.",
                chat_id=chat_id,
            )
            return

        existing = get_unresolved_food(int(unresolved_food_id))

        if existing is None:
            show_unresolved_food_review(chat_id=chat_id)
            return

        updated = update_unresolved_food_details(
            int(unresolved_food_id),
            original_text=text,
            meal_category=(
                interpretation.meal_category
                or existing.get("meal_category")
            ),
            food_name=interpretation.food_name,
            brand=interpretation.brand,
            restaurant=interpretation.restaurant,
            size=interpretation.size,
            quantity=interpretation.quantity,
            quantity_description=(
                interpretation.quantity_description
            ),
        )

        send_telegram_msg(
            "Unknown food details updated.",
            chat_id=chat_id,
        )
        show_unresolved_food_review(
            chat_id=chat_id,
            unresolved_food_id=int(
                updated["unresolved_food_id"]
            ),
            skipped_ids=skipped_ids,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "unresolved_food_review"
        and active_conversation.get("current_step") == "menu"
    ):
        lowered = text.lower().strip()
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        unresolved_food_id = known_data.get(
            "_unresolved_food_id"
        )
        skipped_ids = list(
            known_data.get("_skipped_unresolved_food_ids") or []
        )
        item = (
            get_unresolved_food(int(unresolved_food_id))
            if unresolved_food_id is not None
            else None
        )

        if item is None or item.get("status") != "pending":
            show_unresolved_food_review(chat_id=chat_id)
            return

        if lowered in {"1", "enter nutrition", "nutrition"}:
            item_data = dict(item)
            item_data.update(
                {
                    "_unresolved_food_id": unresolved_food_id,
                    "_entry_date": item.get("entry_date"),
                    "_manual_label_field": "serving_size",
                    "_skipped_unresolved_food_ids": skipped_ids,
                }
            )

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_interpretation",
                current_step="manual_label_entry",
                known_data=item_data,
                missing_fields=[],
                original_message=str(
                    item.get("original_text") or ""
                ),
            )

            send_telegram_msg(
                "What serving size applies to this food?\n"
                "Examples: 28 g, 1 oz, 1 serving.",
                chat_id=chat_id,
            )
            return

        if lowered in {"2", "change", "change name", "edit"}:
            update_conversation(
                chat_id=chat_id,
                current_step="edit_details",
                known_data=known_data,
                missing_fields=[],
            )
            send_telegram_msg(
                "Send the complete corrected food description.\n\n"
                "Example: For breakfast I had one medium apple.",
                chat_id=chat_id,
            )
            return

        if lowered in {"exit", "quit", "stop reviewing"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Unknown-food review closed. No foods were changed.",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "retry", "automatic lookup"}:
            logging.info(
                "Retrying unresolved Food lookup: id=%s "
                "food=%r size=%r brand=%r restaurant=%r",
                unresolved_food_id,
                item.get("food_name"),
                item.get("size"),
                item.get("brand"),
                item.get("restaurant"),
            )

            try:
                provider_result = lookup_official_nutrition(
                    restaurant=item.get("restaurant"),
                    food_name=item.get("food_name"),
                    size=item.get("size"),
                    brand=item.get("brand"),
                )
            except Exception:
                logging.exception(
                    "Unresolved Food automatic lookup failed"
                )
                send_telegram_msg(
                    "The automatic lookup failed. The food remains "
                    "in the queue.",
                    chat_id=chat_id,
                )
                return

            if not provider_result.get("found"):
                provider_notes = [
                    str(note)
                    for note in (
                        provider_result.get("notes") or []
                    )
                    if str(note).strip()
                ]
                logging.info(
                    "Unresolved Food lookup unsupported: id=%s "
                    "missing=%r question=%r notes=%r",
                    unresolved_food_id,
                    provider_result.get("missing_fields"),
                    provider_result.get("clarification_question"),
                    provider_notes,
                )

                explanation = (
                    provider_notes[0]
                    if provider_notes
                    else "No verified match was returned."
                )
                send_telegram_msg(
                    "I still could not verify this food "
                    "automatically.\n\n"
                    f"Reason: {explanation}\n\n"
                    "Choose another option.",
                    chat_id=chat_id,
                )
                return

            provider_food = provider_result["food"]
            provider_nutrition = provider_result["nutrition"]
            verification = provider_result["verification"]

            try:
                saved = add_food_with_nutrition(
                    canonical_name=provider_food["canonical_name"],
                    serving_description=(
                        provider_food["serving_description"]
                    ),
                    serving_amount=provider_food["serving_amount"],
                    serving_unit=provider_food["serving_unit"],
                    verification_status=verification["status"],
                    verification_source=verification["source"],
                    calories=provider_nutrition["calories"],
                    protein_g=provider_nutrition["protein_g"],
                    carbohydrates_g=(
                        provider_nutrition["carbohydrates_g"]
                    ),
                    fat_g=provider_nutrition["fat_g"],
                    fiber_g=provider_nutrition["fiber_g"],
                    sugar_g=provider_nutrition["sugar_g"],
                    sodium_mg=provider_nutrition["sodium_mg"],
                    brand=provider_food["brand"],
                    restaurant=provider_food["restaurant"],
                    food_type=provider_food["food_type"],
                    source_item_id=verification["source_item_id"],
                    source_url=verification["source_url"],
                )
            except Exception:
                logging.exception(
                    "Unresolved verified Food save failed"
                )
                send_telegram_msg(
                    "Nutrition was found, but it could not be saved.",
                    chat_id=chat_id,
                )
                return

            saved_food = saved["food"]
            saved_nutrition = saved["nutrition"] or {}

            try:
                quantity = resolve_non_restaurant_quantity(
                    food_id=saved_food["food_id"],
                    quantity=item.get("quantity"),
                    quantity_description=item.get(
                        "quantity_description"
                    ),
                    size=item.get("size"),
                    serving_amount=saved_food.get("serving_amount"),
                    serving_unit=saved_food.get("serving_unit"),
                )
            except ValueError as error:
                send_telegram_msg(str(error), chat_id=chat_id)
                return

            pending_components = [
                {
                    "role": "Food",
                    "food_id": saved_food["food_id"],
                    "canonical_name": saved_food["canonical_name"],
                    "restaurant": saved_food.get("restaurant"),
                    "size": item.get("size"),
                    "quantity": quantity,
                    "calories": saved_nutrition.get("calories"),
                    "protein_g": saved_nutrition.get("protein_g"),
                    "verification_source": verification["source"],
                }
            ]

            review_data = {
                **dict(item),
                "_unresolved_food_id": unresolved_food_id,
                "_entry_date": item.get("entry_date"),
                "_pending_components": pending_components,
                "_skipped_unresolved_food_ids": skipped_ids,
            }

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_interpretation",
                current_step="nutrition_confirmation",
                known_data=review_data,
                missing_fields=[],
                original_message=str(
                    item.get("original_text") or ""
                ),
            )

            prompt_message_id = send_telegram_msg(
                format_pending_nutrition_confirmation(
                    pending_components,
                    meal_category=item.get("meal_category"),
                    entry_date=item.get("entry_date"),
                ),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_nutrition_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )
            return

        if lowered in {"4", "skip", "skip for now"}:
            skipped_ids.append(int(unresolved_food_id))
            pending = get_pending_unresolved_foods()

            show_unresolved_food_review(
                chat_id=chat_id,
                pending=pending,
                skipped_ids=skipped_ids,
            )
            return

        if lowered in {"5", "cancel", "cancel this food"}:
            set_unresolved_food_status(
                int(unresolved_food_id),
                status="cancelled",
            )

            pending = get_pending_unresolved_foods()

            if pending:
                send_telegram_msg(
                    "Unknown food cancelled.",
                    chat_id=chat_id,
                )
                show_unresolved_food_review(
                    chat_id=chat_id,
                    pending=pending,
                    skipped_ids=skipped_ids,
                )
            else:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Unknown food cancelled. The queue is empty.",
                    chat_id=chat_id,
                )
            return

        send_telegram_msg(
            "Please choose 1, 2, 3, 4, or 5.",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "portion_profile_clarification"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        food_id = known_data.get("_portion_profile_food_id")
        phrase = known_data.get("_portion_profile_phrase")

        cleaned = text.strip().lower()

        match = re.fullmatch(
            r"((?:\d+(?:\.\d+)?)|(?:\.\d+))\s*"
            r"(serving|servings|g|gram|grams|grm|"
            r"oz|ounce|ounces)",
            cleaned,
        )

        if match is None:
            send_telegram_msg(
                "Please enter the amount as grams, ounces, "
                "or servings.\n\n"
                "Examples: 28 g, 1 oz, 1.5 servings.",
                chat_id=chat_id,
            )
            return

        amount = float(match.group(1))
        unit = match.group(2)

        if amount <= 0:
            send_telegram_msg(
                "The amount must be greater than zero.",
                chat_id=chat_id,
            )
            return

        if not food_id or not phrase:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "The portion setup could not be completed. "
                "Please send the food again.",
                chat_id=chat_id,
            )
            return

        try:
            save_portion_profile(
                food_id=int(food_id),
                phrase=str(phrase),
                estimated_amount=amount,
                estimated_unit=unit,
                user_confirmed=True,
            )
        except Exception:
            logging.exception(
                "Saving portion profile failed"
            )
            send_telegram_msg(
                "I could not save that portion profile.",
                chat_id=chat_id,
            )
            return

        known_data["quantity_description"] = str(phrase)

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        model_data = {
            field: value
            for field, value in known_data.items()
            if field in FoodInterpretation.model_fields
        }

        interpretation = FoodInterpretation.model_validate(
            model_data
        )

        send_telegram_msg(
            "Saved your portion profile.\n\n"
            + format_food_interpretation(
                interpretation,
                entry_date=known_data.get("_entry_date"),
            ),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "manual_label_offer"
    ):
        lowered = text.lower().strip()
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        if lowered in {
            "1",
            "enter",
            "enter package label nutrition",
            "manual",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "serving_size",
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "What serving size is listed on the package?\n\n"
                "Example: 28 g",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "2",
            "try again",
            "different description",
        }:
            prompt_for_corrected_food(
                chat_id=chat_id,
                known_data=known_data,
                message=(
                    "Send the food description again with any "
                    "additional brand, flavor, or product details."
                ),
            )
            return

        if lowered in {
            "3",
            "save for later",
            "later",
            "save",
        }:
            original_message = (
                active_conversation.get("original_message")
                or known_data.get("food_name")
                or "Unresolved food"
            )

            try:
                target_entry_date = parse_food_entry_date(
                    known_data.get("_entry_date"),
                    default=datetime.now(PACIFIC_TZ).date(),
                )
                saved_unresolved = save_unresolved_food(
                    entry_date=target_entry_date.isoformat(),
                    meal_category=known_data.get(
                        "meal_category"
                    ),
                    original_text=original_message,
                    food_name=known_data.get("food_name"),
                    brand=known_data.get("brand"),
                    restaurant=known_data.get(
                        "restaurant"
                    ),
                    size=known_data.get("size"),
                    quantity=known_data.get("quantity"),
                    quantity_description=known_data.get(
                        "quantity_description"
                    ),
                )
            except Exception:
                logging.exception(
                    "Saving unresolved Food failed"
                )
                send_telegram_msg(
                    "I could not save that food for later.",
                    chat_id=chat_id,
                )
                return

            cancel_conversation(chat_id)

            send_telegram_msg(
                "Saved for later. No calories or nutrients "
                "were added yet.\n\n"
                "You can finish this food later with "
                "'update unknown foods'.",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "4",
            "cancel",
            "no",
        }:
            cancel_conversation(chat_id)

            send_telegram_msg(
                "Food entry cancelled.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please choose:\n"
            "1. Enter package label nutrition\n"
            "2. Try a different description\n"
            "3. Save for later\n"
            "4. Cancel",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "manual_label_entry"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        current_field = known_data.get(
            "_manual_label_field"
        )

        if current_field == "serving_size":
            cleaned = text.strip().lower()

            match = re.fullmatch(
                r"(\d+(?:\.\d+)?)\s*"
                r"(g|gram|grams|grm|oz|ounce|ounces|"
                r"serving|servings)",
                cleaned,
            )

            if match is None:
                send_telegram_msg(
                    "Please enter the serving size as grams, "
                    "ounces, or servings.\n\n"
                    "Examples: 28 g, 1 oz, 1 serving.",
                    chat_id=chat_id,
                )
                return

            amount = float(match.group(1))
            unit = match.group(2)

            if amount <= 0:
                send_telegram_msg(
                    "The serving size must be greater than zero.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "calories",
                    "_manual_label_serving_amount": amount,
                    "_manual_label_serving_unit": unit,
                    "_manual_label_serving_description": (
                        text.strip()
                    ),
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many calories are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "calories":
            cleaned = text.strip()

            try:
                calories = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter calories as a number. "
                    "Example: 130",
                    chat_id=chat_id,
                )
                return

            if calories < 0:
                send_telegram_msg(
                    "Calories cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "protein_g",
                    "_manual_label_calories": calories,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of protein are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "protein_g":
            cleaned = text.strip()

            try:
                protein_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter protein as a number. "
                    "Example: 2",
                    chat_id=chat_id,
                )
                return

            if protein_g < 0:
                send_telegram_msg(
                    "Protein cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "carbohydrates_g",
                    "_manual_label_protein_g": protein_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of carbohydrates are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "carbohydrates_g":
            cleaned = text.strip()

            try:
                carbohydrates_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter carbohydrates as a number. "
                    "Example: 19",
                    chat_id=chat_id,
                )
                return

            if carbohydrates_g < 0:
                send_telegram_msg(
                    "Carbohydrates cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "fat_g",
                    "_manual_label_carbohydrates_g": carbohydrates_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of fat are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "fat_g":
            cleaned = text.strip()

            try:
                fat_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter fat as a number. "
                    "Example: 6",
                    chat_id=chat_id,
                )
                return

            if fat_g < 0:
                send_telegram_msg(
                    "Fat cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "fiber_g",
                    "_manual_label_fat_g": fat_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of fiber are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "fiber_g":
            cleaned = text.strip()

            try:
                fiber_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter fiber as a number. "
                    "Example: 0",
                    chat_id=chat_id,
                )
                return

            if fiber_g < 0:
                send_telegram_msg(
                    "Fiber cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "sugar_g",
                    "_manual_label_fiber_g": fiber_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of sugar are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "sugar_g":
            cleaned = text.strip()

            try:
                sugar_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter sugar as a number. "
                    "Example: 9",
                    chat_id=chat_id,
                )
                return

            if sugar_g < 0:
                send_telegram_msg(
                    "Sugar cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "sodium_mg",
                    "_manual_label_sugar_g": sugar_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many milligrams of sodium are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "sodium_mg":
            cleaned = text.strip()

            try:
                sodium_mg = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter sodium as a number. "
                    "Example: 180",
                    chat_id=chat_id,
                )
                return

            if sodium_mg < 0:
                send_telegram_msg(
                    "Sodium cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_confirmation",
                known_data={
                    **known_data,
                    "_manual_label_field": None,
                    "_manual_label_sodium_mg": sodium_mg,
                },
                missing_fields=[],
            )

            serving_description = known_data.get(
                "_manual_label_serving_description"
            )

            send_telegram_msg(
                "Package label entered:\n\n"
                f"Serving: {serving_description}\n"
                f"Calories: {known_data.get('_manual_label_calories'):g}\n"
                f"Protein: {known_data.get('_manual_label_protein_g'):g} g\n"
                f"Carbohydrates: {known_data.get('_manual_label_carbohydrates_g'):g} g\n"
                f"Fat: {known_data.get('_manual_label_fat_g'):g} g\n"
                f"Fiber: {known_data.get('_manual_label_fiber_g'):g} g\n"
                f"Sugar: {known_data.get('_manual_label_sugar_g'):g} g\n"
                f"Sodium: {sodium_mg:g} mg\n\n"
                "1. Save\n"
                "2. Edit\n"
                "3. Cancel",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Manual label entry is incomplete. "
            "Please start the food entry again.",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "manual_label_confirmation"
    ):
        lowered = text.lower().strip()
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        if lowered in {"1", "save", "yes"}:
            food_name = known_data.get("food_name")
            brand = known_data.get("brand")
            meal_category = known_data.get("meal_category")

            serving_amount = known_data.get(
                "_manual_label_serving_amount"
            )
            serving_unit = known_data.get(
                "_manual_label_serving_unit"
            )
            serving_description = known_data.get(
                "_manual_label_serving_description"
            )

            if (
                not food_name
                or not meal_category
                or serving_amount is None
                or not serving_unit
                or not serving_description
            ):
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "The package-label entry is incomplete, "
                    "so nothing was saved.",
                    chat_id=chat_id,
                )
                return

            try:
                saved = add_food_with_nutrition(
                    canonical_name=food_name,
                    serving_description=serving_description,
                    serving_amount=float(serving_amount),
                    serving_unit=str(serving_unit),
                    verification_status="verified",
                    verification_source="user_package_label",
                    calories=known_data.get(
                        "_manual_label_calories"
                    ),
                    protein_g=known_data.get(
                        "_manual_label_protein_g"
                    ),
                    carbohydrates_g=known_data.get(
                        "_manual_label_carbohydrates_g"
                    ),
                    fat_g=known_data.get(
                        "_manual_label_fat_g"
                    ),
                    fiber_g=known_data.get(
                        "_manual_label_fiber_g"
                    ),
                    sugar_g=known_data.get(
                        "_manual_label_sugar_g"
                    ),
                    sodium_mg=known_data.get(
                        "_manual_label_sodium_mg"
                    ),
                    brand=brand,
                    restaurant=None,
                    food_type="food",
                    source_item_id=None,
                    source_url=None,
                )
            except Exception:
                logging.exception(
                    "Manual package-label Food save failed"
                )
                send_telegram_msg(
                    "I could not save the package-label nutrition.",
                    chat_id=chat_id,
                )
                return

            saved_food = saved["food"]
            saved_nutrition = saved["nutrition"] or {}

            try:
                quantity = resolve_non_restaurant_quantity(
                    food_id=saved_food["food_id"],
                    quantity=known_data.get("quantity"),
                    quantity_description=known_data.get(
                        "quantity_description"
                    ),
                    size=known_data.get("size"),
                    serving_amount=saved_food.get(
                        "serving_amount"
                    ),
                    serving_unit=saved_food.get(
                        "serving_unit"
                    ),
                )
            except ValueError as error:
                send_telegram_msg(
                    str(error),
                    chat_id=chat_id,
                )
                return

            pending_components = [
                {
                    "role": "Food",
                    "food_id": saved_food["food_id"],
                    "canonical_name": saved_food[
                        "canonical_name"
                    ],
                    "restaurant": None,
                    "size": None,
                    "quantity": quantity,
                    "calories": saved_nutrition.get(
                        "calories"
                    ),
                    "protein_g": saved_nutrition.get(
                        "protein_g"
                    ),
                    "verification_source": (
                        "user_package_label"
                    ),
                }
            ]

            update_conversation(
                chat_id=chat_id,
                current_step="nutrition_confirmation",
                known_data={
                    **known_data,
                    "_pending_components": pending_components,
                },
                missing_fields=[],
            )

            prompt_message_id = send_telegram_msg(
                format_pending_nutrition_confirmation(
                    pending_components,
                    meal_category=meal_category,
                    entry_date=known_data.get("_entry_date"),
                ),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_nutrition_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        if lowered in {"2", "edit"}:
            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "serving_size",
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "Let's re-enter the package label.\n\n"
                "What serving size is listed on the package?\n"
                "Example: 28 g",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled. Nothing was saved.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please choose:\n"
            "1. Save\n"
            "2. Edit\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "nutrition_confirmation"
    ):
        lowered = text.lower().strip()
        known_data = active_conversation.get("known_data") or {}

        prompt_message_id = known_data.get(
            "_nutrition_prompt_message_id"
        )

        if (
            prompt_message_id is not None
            and message_id is not None
            and int(message_id) <= int(prompt_message_id)
        ):
            logging.info(
                "Ignored Telegram reply %s because it predates "
                "nutrition prompt %s",
                message_id,
                prompt_message_id,
            )
            return

        pending_components = (
            known_data.get("_pending_components") or []
        )
        meal_category = known_data.get("meal_category")
        restaurant = known_data.get("restaurant")
        original_message = active_conversation.get(
            "original_message"
        )
        target_entry_date = datetime.now(PACIFIC_TZ).date()
        stored_entry_date = known_data.get("_entry_date")

        if stored_entry_date:
            try:
                target_entry_date = datetime.strptime(
                    str(stored_entry_date),
                    "%Y-%m-%d",
                ).date()
            except ValueError:
                logging.warning(
                    "Invalid unresolved Food entry date: %s",
                    stored_entry_date,
                )

        if known_data.get("_duplicate_warning_pending"):
            if lowered in {
                "1",
                "yes",
                "y",
                "log another",
                "log it again",
            }:
                known_data = {
                    **known_data,
                    "_duplicate_warning_pending": False,
                    "_duplicate_override": True,
                }

                update_conversation(
                    chat_id=chat_id,
                    known_data=known_data,
                )

            elif lowered in {
                "2",
                "no",
                "n",
                "cancel",
                "keep first",
            }:
                cancel_conversation(chat_id)

                send_telegram_msg(
                    "Duplicate entry cancelled. "
                    "The earlier food entry was kept.",
                    chat_id=chat_id,
                )
                return

            else:
                send_telegram_msg(
                    "It looks like this food was just logged.\n\n"
                    "1. Yes, log another\n"
                    "2. No, keep only the first one",
                    chat_id=chat_id,
                )
                return

        if lowered in {"1", "log", "log it", "correct", "yes"}:
            if not pending_components or not meal_category:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "The pending nutrition record is incomplete, "
                    "so nothing was logged.",
                    chat_id=chat_id,
                )
                return

            duplicate_override = bool(
                known_data.get("_duplicate_override")
            )

            if not duplicate_override:
                duplicate_component = None
                duplicate_entry = None
                for component in pending_components:
                    duplicate = find_recent_duplicate_entry(
                        entry_date=target_entry_date,
                        meal_category=meal_category,
                        food_id=int(component["food_id"]),
                        quantity=float(
                            component.get("quantity") or 1.0
                        ),
                        window_minutes=5,
                    )

                    if duplicate is not None:
                        duplicate_component = component
                        duplicate_entry = duplicate
                        break

                if duplicate_entry is not None:
                    display_name = format_food_display_name(
                        canonical_name=duplicate_component[
                            "canonical_name"
                        ],
                        restaurant=duplicate_component.get(
                            "restaurant"
                        ),
                        size=duplicate_component.get("size"),
                    )

                    update_conversation(
                        chat_id=chat_id,
                        known_data={
                            **known_data,
                            "_duplicate_warning_pending": True,
                        },
                    )

                    send_telegram_msg(
                        "It looks like you just logged this:\n\n"
                        f"{display_name}\n"
                        f"{format_display_number(float(duplicate_entry['calories'] or 0), decimals=0)} calories\n\n"
                        "Log another one?\n"
                        "1. Yes, log another\n"
                        "2. No, keep only the first one",
                        chat_id=chat_id,
                    )
                    return

            logged_entries = []

            try:
                for component in pending_components:
                    entry = add_food_entry(
                        entry_date=target_entry_date,
                        meal_category=meal_category,
                        food_id=int(component["food_id"]),
                        quantity=float(
                            component.get("quantity") or 1.0
                        ),
                        logging_source="telegram_ai",
                        original_text=original_message,
                        quantity_is_estimated=False,
                        user_confirmed=True,
                    )

                    logged_entries.append(
                        {
                            **component,
                            "entry": entry,
                        }
                    )
            except Exception as error:
                logging.exception(
                    "Pending nutrition logging failed"
                )

                for logged in logged_entries:
                    try:
                        delete_food_entry(
                            logged["entry"]["food_entry_id"]
                        )
                    except Exception:
                        logging.exception(
                            "Pending nutrition rollback failed"
                        )

                cancel_conversation(chat_id)

                if isinstance(error, ValueError):
                    failure_message = (
                        "I couldn't log that food: "
                        f"{error}\n\n"
                        "Nothing was logged. Send the food again "
                        "to start a new entry."
                    )
                else:
                    failure_message = (
                        "The food could not be completely logged. "
                        "Any partial ledger entries were removed.\n\n"
                        "The failed entry was closed, so you can send "
                        "the food again as a new message."
                    )

                send_telegram_msg(
                    failure_message,
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(
                    target_entry_date
                )
            except Exception:
                logging.exception(
                    "Food Ledger Google Sheet sync failed"
                )
                send_telegram_msg(
                    "Food was logged, but the Google Sheet "
                    "nutrition totals could not be updated.",
                    chat_id=chat_id,
                )

            unresolved_food_id = known_data.get(
                "_unresolved_food_id"
            )

            if unresolved_food_id is not None:
                set_unresolved_food_status(
                    int(unresolved_food_id),
                    status="resolved",
                )
                complete_conversation(chat_id)

                send_telegram_msg(
                    append_goal_calorie_progress(
                        "Food logged for "
                        f"{target_entry_date.isoformat()} "
                        f"({meal_category.title()}).",
                        target_entry_date,
                    ),
                    chat_id=chat_id,
                )

                pending = get_pending_unresolved_foods()
                skipped_ids = list(
                    known_data.get(
                        "_skipped_unresolved_food_ids"
                    )
                    or []
                )

                if pending:
                    show_unresolved_food_review(
                        chat_id=chat_id,
                        pending=pending,
                        skipped_ids=skipped_ids,
                    )
                else:
                    cancel_conversation(chat_id)
                    send_telegram_msg(
                        "All unknown foods have been reviewed.",
                        chat_id=chat_id,
                    )
                return

            complete_conversation(chat_id)

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_meal",
                current_step=(
                    "restaurant_anything_else"
                    if restaurant
                    else "meal_anything_else"
                ),
                known_data={
                    "meal_category": meal_category,
                    "restaurant": restaurant,
                    "_entry_date": target_entry_date.isoformat(),
                },
                missing_fields=[],
                original_message=original_message,
            )

            lines = [
                (
                    "Meal logged."
                    if len(logged_entries) > 1
                    else "Food logged."
                ),
                "",
            ]

            for component in logged_entries:
                entry = component["entry"]

                display_name = format_food_display_name(
                    canonical_name=component["canonical_name"],
                    restaurant=component.get("restaurant"),
                    size=component.get("size"),
                )

                line = (
                    f"{component.get('role', 'Food')}: "
                    f"{display_name}"
                )

                if entry.get("calories") is not None:
                    line += (
                        " — "
                        + format_display_number(
                            float(entry["calories"]),
                            decimals=0,
                        )
                        + " calories"
                    )

                lines.append(line)

            goal_progress = format_goal_calorie_progress(
                target_entry_date
            )
            if goal_progress:
                lines.extend(["", goal_progress])

            if (
                target_entry_date
                != datetime.now(PACIFIC_TZ).date()
            ):
                try:
                    updated_totals = format_daily_food_totals(
                        target_entry_date
                    )
                except Exception:
                    logging.exception(
                        "Previous-day Food totals failed"
                    )
                    updated_totals = (
                        "The food was logged for "
                        f"{format_food_entry_date(target_entry_date)}, "
                        "but I could not calculate the updated totals."
                    )
                lines.extend(["", updated_totals])

            lines.extend(
                [
                    "",
                    (
                        f"Anything else from {restaurant}?"
                        if restaurant
                        else (
                            f"Anything else for "
                            f"{meal_category.title()}?"
                        )
                    ),
                    "1. Yes",
                    "2. No",
                ]
            )

            prompt_message_id = send_telegram_msg(
                "\n".join(lines),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_food_meal_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        if lowered in {
            "2",
            "enter custom nutrition",
            "custom nutrition",
            "custom",
        }:
            if len(pending_components) != 1 or not meal_category:
                send_telegram_msg(
                    "Custom nutrition can currently replace one food "
                    "at a time. Please edit this entry and send one "
                    "food description.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "serving_size",
                    "_custom_nutrition_override": True,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "Enter custom nutrition for this food. The verified "
                "match will not be logged.\n\n"
                "What serving size applies?\n"
                "Examples: 1 serving, 28 g, or 1 oz.",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "edit"}:
            prompt_for_corrected_food(
                chat_id=chat_id,
                known_data=known_data,
            )
            return

        if lowered in {"4", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled. Nothing was logged.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please reply:\n"
            "1. Log It\n"
            "2. Enter custom nutrition\n"
            "3. Edit\n"
            "4. Cancel",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_meal"
        and active_conversation.get("current_step")
        == "restaurant_anything_else"
    ):
        lowered = text.lower().strip()
        known_data = active_conversation.get("known_data") or {}
        meal_category = known_data.get("meal_category")
        entry_date = parse_food_entry_date(
            known_data.get("_entry_date"),
            default=datetime.now(PACIFIC_TZ).date(),
        )
        restaurant = known_data.get("restaurant")

        prompt_message_id = known_data.get(
            "_food_meal_prompt_message_id"
        )

        if (
            prompt_message_id is not None
            and message_id is not None
            and int(message_id) <= int(prompt_message_id)
        ):
            logging.info(
                "Ignored Telegram reply %s because it predates "
                "food-meal prompt %s",
                message_id,
                prompt_message_id,
            )
            return

        if lowered in {
            "1",
            "yes",
            "y",
            "add another food",
            "another",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="awaiting_food",
                known_data={
                    "meal_category": meal_category,
                    "restaurant": restaurant,
                    "_entry_date": entry_date.isoformat(),
                },
                missing_fields=[],
            )

            send_telegram_msg(
                f"What else did you have from {restaurant}?",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "2",
            "no",
            "n",
            "finished",
            "done",
            "nothing else",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="meal_anything_else",
                known_data={
                    "meal_category": meal_category,
                    "restaurant": None,
                    "_entry_date": entry_date.isoformat(),
                },
                missing_fields=[],
            )

            prompt_message_id = send_telegram_msg(
                f"Anything else for {meal_category.title()}?\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_food_meal_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        prompt_message_id = send_telegram_msg(
            f"Anything else from {restaurant}?\n"
            "1. Yes\n"
            "2. No",
            chat_id=chat_id,
        )

        if isinstance(prompt_message_id, int):
            update_conversation(
                chat_id=chat_id,
                known_data={
                    "_food_meal_prompt_message_id": (
                        prompt_message_id
                    ),
                },
            )

        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_meal"
        and active_conversation.get("current_step")
        == "meal_anything_else"
    ):
        lowered = text.lower().strip()
        known_data = active_conversation.get("known_data") or {}
        meal_category = known_data.get("meal_category")
        entry_date = parse_food_entry_date(
            known_data.get("_entry_date"),
            default=datetime.now(PACIFIC_TZ).date(),
        )

        prompt_message_id = known_data.get(
            "_food_meal_prompt_message_id"
        )

        if (
            prompt_message_id is not None
            and message_id is not None
            and int(message_id) <= int(prompt_message_id)
        ):
            logging.info(
                "Ignored Telegram reply %s because it predates "
                "food-meal prompt %s",
                message_id,
                prompt_message_id,
            )
            return

        if lowered in {
            "1",
            "yes",
            "y",
            "add another food",
            "another",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="awaiting_food",
                known_data={
                    "meal_category": meal_category,
                    "restaurant": None,
                    "_entry_date": entry_date.isoformat(),
                },
                missing_fields=[],
            )

            send_telegram_msg(
                f"What else did you have for "
                f"{meal_category.title()}?",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "2",
            "no",
            "n",
            "finished",
            "done",
            "nothing else",
        }:
            complete_conversation(chat_id)

            try:
                totals_message = format_daily_food_totals(
                    entry_date
                )
            except Exception:
                logging.exception(
                    "Daily Food Ledger totals failed"
                )
                send_telegram_msg(
                    f"{meal_category.title()} is finished, "
                    "but I could not calculate the food totals.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                f"{meal_category.title()} is finished.\n\n"
                f"{totals_message}",
                chat_id=chat_id,
            )
            return

        prompt_message_id = send_telegram_msg(
            f"Anything else for {meal_category.title()}?\n"
            "1. Yes\n"
            "2. No",
            chat_id=chat_id,
        )

        if isinstance(prompt_message_id, int):
            update_conversation(
                chat_id=chat_id,
                known_data={
                    "_food_meal_prompt_message_id": (
                        prompt_message_id
                    ),
                },
            )

        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_meal"
        and active_conversation.get("current_step")
        == "awaiting_food"
    ):
        meal_context = (
            active_conversation.get("known_data") or {}
        )
        meal_category = meal_context.get("meal_category")
        restaurant_context = meal_context.get("restaurant")
        entry_date = parse_food_entry_date(
            meal_context.get("_entry_date")
        )

        try:
            interpretation = interpret_food_message(text)
        except Exception:
            logging.exception(
                "Additional meal food interpretation failed"
            )
            send_telegram_msg(
                "I could not understand that food. "
                "Please describe it again.",
                chat_id=chat_id,
            )
            return

        if not interpretation.is_food_logging_request:
            try:
                saved_name_match = resolve_food(
                    food_name=text,
                    serving_description="standard",
                    brand=None,
                    restaurant=restaurant_context,
                )
            except Exception:
                logging.exception(
                    "Additional meal Saved Food lookup failed"
                )
                saved_name_match = {"found": False}

            if not saved_name_match.get("found"):
                send_telegram_msg(
                    "Please send the food you want to add.",
                    chat_id=chat_id,
                )
                return

            interpretation = FoodInterpretation(
                is_food_logging_request=True,
                food_name=text.strip(),
                quantity=None,
                meal_category=meal_category,
                missing_fields=["quantity"],
                assumptions=[],
                confidence=1.0,
                clarification_question="How many did you have?",
            )

        interpretation.meal_category = meal_category

        if (
            restaurant_context
            and not interpretation.restaurant
        ):
            interpretation.restaurant = restaurant_context

        interpretation = clean_interpretation_missing_fields(
            interpretation
        )

        interpretation.missing_fields = [
            field
            for field in interpretation.missing_fields
            if field != "meal_category"
        ]

        if not interpretation.missing_fields:
            interpretation.clarification_question = None

        start_conversation(
            chat_id=chat_id,
            conversation_type="food_interpretation",
            current_step=(
                "confirmation"
                if not interpretation.missing_fields
                else "clarification"
            ),
            known_data={
                **interpretation.model_dump(),
                **(
                    {"_entry_date": entry_date.isoformat()}
                    if entry_date is not None
                    else {}
                ),
            },
            missing_fields=interpretation.missing_fields,
            original_message=text,
        )

        send_telegram_msg(
            format_food_interpretation(
                interpretation,
                entry_date=entry_date,
            ),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "clarification"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        previous_missing = list(
            active_conversation.get("missing_fields")
            or known_data.get("missing_fields")
            or []
        )

        if "quantity_description" in previous_missing:
            cleaned_amount = text.strip().lower()

            valid_amount = (
                re.fullmatch(
                    r"\d+(?:\.\d+)?\s*"
                    r"(?:serving|servings|g|gram|grams|grm|"
                    r"oz|ounce|ounces)",
                    cleaned_amount,
                )
                is not None
                or "handful" in cleaned_amount
            )

            if not valid_amount:
                send_telegram_msg(
                    "How much did you have? Reply with an amount such as "
                    "1 serving, 1 cup, 4 oz, or 100 g. Reply Cancel to "
                    "stop without logging.",
                    chat_id=chat_id,
                )
                return

            if valid_amount:
                known_data["quantity_description"] = text.strip()

                remaining_missing = [
                    field
                    for field in previous_missing
                    if field != "quantity_description"
                ]

                known_data["missing_fields"] = remaining_missing

                if remaining_missing:
                    question_by_field = {
                        "restaurant": "What restaurant was this from?",
                        "brand": "What brand was it?",
                        "food_name": "What food did you have?",
                        "size": "What size was it?",
                        "quantity": "How many did you have?",
                        "quantity_description": (
                            "How much did you have? Reply with an amount "
                            "such as 1 serving, 1 cup, 4 oz, or 100 g."
                        ),
                        "meal_category": "Which meal was this for?",
                        "drink": "What drink did you have?",
                        "drink_detail": "What drink did you have?",
                        "food_name_detail": (
                            "Which exact menu item did you have?"
                        ),
                    }

                    next_field = remaining_missing[0]

                    update_conversation(
                        chat_id=chat_id,
                        current_step="clarification",
                        known_data=known_data,
                        missing_fields=remaining_missing,
                    )

                    send_telegram_msg(
                        question_by_field.get(
                            next_field,
                            "What detail should I add?",
                        ),
                        chat_id=chat_id,
                    )
                    return

                known_data["clarification_question"] = None

                update_conversation(
                    chat_id=chat_id,
                    current_step="confirmation",
                    known_data=known_data,
                    missing_fields=[],
                )

                model_data = {
                    field: value
                    for field, value in known_data.items()
                    if field in FoodInterpretation.model_fields
                }

                interpretation = FoodInterpretation.model_validate(
                    model_data
                )

                send_telegram_msg(
                    format_food_interpretation(
                        interpretation,
                        entry_date=known_data.get("_entry_date"),
                    ),
                    chat_id=chat_id,
                )
                return

        base_text = (
            known_data.get("_accumulated_text")
            or active_conversation.get("original_message")
            or known_data.get("food_name")
            or ""
        )

        accumulated_text = (
            f"{base_text}. Additional detail: {text}"
            if base_text
            else text
        )

        try:
            clarification = interpret_food_message(
                accumulated_text
            )
        except Exception:
            logging.exception(
                "Food clarification interpretation failed"
            )
            send_telegram_msg(
                "I could not understand that clarification. "
                "Please try again.",
                chat_id=chat_id,
            )
            return

        merge_fields = (
            "restaurant",
            "brand",
            "food_name",
            "size",
            "quantity",
            "quantity_description",
            "meal_category",
            "drink",
        )

        for field in merge_fields:
            existing_value = known_data.get(field)
            new_value = getattr(clarification, field)

            if existing_value in (None, "") and new_value not in (
                None,
                "",
            ):
                known_data[field] = new_value

        if "food_name_detail" in previous_missing:
            clarified_food_name = clarification.food_name

            if clarified_food_name not in (None, ""):
                known_data["food_name"] = clarified_food_name

        if "drink_detail" in previous_missing:
            clarified_drink = (
                clarification.drink
                or clarification.food_name
            )

            if clarified_drink not in (
                None,
                "",
                "drink",
                "beverage",
            ):
                known_data["drink"] = clarified_drink
                known_data["food_name"] = clarified_drink

        current_missing_field = (
            previous_missing[0]
            if previous_missing
            else None
        )

        if known_data.get("is_combo_meal"):
            lowered_answer = text.lower().strip()

            size_words = {
                "small",
                "medium",
                "large",
            }

            detected_size = clarification.size

            if detected_size in (None, ""):
                for size_word in size_words:
                    if size_word in lowered_answer.split():
                        detected_size = size_word
                        break

            if (
                current_missing_field == "size"
                and detected_size not in (None, "")
            ):
                known_data["size"] = detected_size
                known_data["combo_side_size"] = detected_size
                known_data["combo_drink_size"] = detected_size

            clarified_combo_drink = clarification.drink

            if (
                current_missing_field == "drink"
                and clarified_combo_drink in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                clarified_combo_drink = text.strip()

            if (
                current_missing_field == "drink"
                and clarified_combo_drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarified_combo_drink
                known_data["combo_drink"] = clarified_combo_drink

            if (
                current_missing_field == "size"
                and clarification.drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarification.drink
                known_data["combo_drink"] = clarification.drink

        known_data["_accumulated_text"] = accumulated_text

        candidate_missing = []

        for field in (
            previous_missing
            + list(clarification.missing_fields)
        ):
            if field not in candidate_missing:
                candidate_missing.append(field)

        clarification_priority = (
            "restaurant",
            "brand",
            "size",
            "quantity",
            "quantity_description",
            "drink_detail",
            "drink",
            "meal_category",
            "food_name_detail",
            "food_name",
        )

        unresolved_fields = {
            field
            for field in candidate_missing
            if (
                field in {
                    "food_name_detail",
                    "drink_detail",
                }
                or known_data.get(field) in (None, "")
            )
        }

        if (
            "drink_detail" in unresolved_fields
            and clarification.drink not in (
                None,
                "",
                "drink",
                "beverage",
            )
        ):
            unresolved_fields.remove("drink_detail")

        if (
            "food_name_detail" in unresolved_fields
            and clarification.food_name not in (None, "")
            and normalize_signature_food(
                clarification.food_name
            ) not in {
                "burger",
                "burgers",
                "burrito",
                "burritos",
                "cheeseburger",
                "cheeseburgers",
                "fries",
                "french fries",
                "pizza",
                "sandwich",
                "sandwiches",
                "salad",
                "salads",
                "taco",
                "tacos",
            }
        ):
            unresolved_fields.remove("food_name_detail")

        remaining_missing = [
            field
            for field in clarification_priority
            if field in unresolved_fields
        ]

        remaining_missing.extend(
            field
            for field in candidate_missing
            if (
                field in unresolved_fields
                and field not in remaining_missing
            )
        )

        known_data["missing_fields"] = remaining_missing

        question_by_field = {
            "restaurant": "What restaurant was this from?",
            "brand": "What brand was it?",
            "food_name": "What food did you have?",
            "size": "What size was it?",
            "quantity": "How many did you have?",
            "quantity_description": (
                "How much did you have? Reply with an amount such as "
                "1 serving, 1 cup, 4 oz, or 100 g."
            ),
            "meal_category": "Which meal was this for?",
            "drink": "What drink did you have?",
            "drink_detail": "What drink did you have?",
            "food_name_detail": (
                "Which exact menu item did you have?"
            ),
        }

        if remaining_missing:
            next_field = remaining_missing[0]
            question = question_by_field.get(
                next_field,
                "What detail should I add?",
            )

            known_data["clarification_question"] = question

            update_conversation(
                chat_id=chat_id,
                current_step="clarification",
                known_data=known_data,
                missing_fields=remaining_missing,
            )

            send_telegram_msg(
                question,
                chat_id=chat_id,
            )
            return

        known_data["clarification_question"] = None

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        model_data = {
            field: value
            for field, value in known_data.items()
            if field in FoodInterpretation.model_fields
        }

        interpretation = FoodInterpretation.model_validate(
            model_data
        )

        send_telegram_msg(
            format_food_interpretation(
                interpretation,
                entry_date=known_data.get("_entry_date"),
            ),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "clarification"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        previous_missing = list(
            active_conversation.get("missing_fields")
            or known_data.get("missing_fields")
            or []
        )

        if "quantity_description" in previous_missing:
            cleaned_amount = text.strip().lower()

            valid_amount = (
                re.fullmatch(
                    r"\d+(?:\.\d+)?\s*"
                    r"(?:serving|servings|g|gram|grams|grm|"
                    r"oz|ounce|ounces)",
                    cleaned_amount,
                )
                is not None
                or "handful" in cleaned_amount
            )

            if not valid_amount:
                send_telegram_msg(
                    "How much did you have? Reply with an amount such as "
                    "1 serving, 1 cup, 4 oz, or 100 g. Reply Cancel to "
                    "stop without logging.",
                    chat_id=chat_id,
                )
                return

            if valid_amount:
                known_data["quantity_description"] = text.strip()

                remaining_missing = [
                    field
                    for field in previous_missing
                    if field != "quantity_description"
                ]

                known_data["missing_fields"] = remaining_missing

                if remaining_missing:
                    question_by_field = {
                        "restaurant": "What restaurant was this from?",
                        "brand": "What brand was it?",
                        "food_name": "What food did you have?",
                        "size": "What size was it?",
                        "quantity": "How many did you have?",
                        "quantity_description": (
                            "How much did you have? Reply with an amount "
                            "such as 1 serving, 1 cup, 4 oz, or 100 g."
                        ),
                        "meal_category": "Which meal was this for?",
                        "drink": "What drink did you have?",
                        "drink_detail": "What drink did you have?",
                        "food_name_detail": (
                            "Which exact menu item did you have?"
                        ),
                    }

                    next_field = remaining_missing[0]

                    update_conversation(
                        chat_id=chat_id,
                        current_step="clarification",
                        known_data=known_data,
                        missing_fields=remaining_missing,
                    )

                    send_telegram_msg(
                        question_by_field.get(
                            next_field,
                            "What detail should I add?",
                        ),
                        chat_id=chat_id,
                    )
                    return

                known_data["clarification_question"] = None

                update_conversation(
                    chat_id=chat_id,
                    current_step="confirmation",
                    known_data=known_data,
                    missing_fields=[],
                )

                model_data = {
                    field: value
                    for field, value in known_data.items()
                    if field in FoodInterpretation.model_fields
                }

                interpretation = FoodInterpretation.model_validate(
                    model_data
                )

                send_telegram_msg(
                    format_food_interpretation(
                        interpretation,
                        entry_date=known_data.get("_entry_date"),
                    ),
                    chat_id=chat_id,
                )
                return

        base_text = (
            known_data.get("_accumulated_text")
            or active_conversation.get("original_message")
            or known_data.get("food_name")
            or ""
        )

        accumulated_text = (
            f"{base_text}. Additional detail: {text}"
            if base_text
            else text
        )

        try:
            clarification = interpret_food_message(
                accumulated_text
            )
        except Exception:
            logging.exception(
                "Food clarification interpretation failed"
            )
            send_telegram_msg(
                "I could not understand that clarification. "
                "Please try again.",
                chat_id=chat_id,
            )
            return

        merge_fields = (
            "restaurant",
            "brand",
            "food_name",
            "size",
            "quantity",
            "quantity_description",
            "meal_category",
            "drink",
        )

        for field in merge_fields:
            existing_value = known_data.get(field)
            new_value = getattr(clarification, field)

            if existing_value in (None, "") and new_value not in (
                None,
                "",
            ):
                known_data[field] = new_value

        if "food_name_detail" in previous_missing:
            clarified_food_name = clarification.food_name

            if clarified_food_name not in (None, ""):
                known_data["food_name"] = clarified_food_name

        if "drink_detail" in previous_missing:
            clarified_drink = (
                clarification.drink
                or clarification.food_name
            )

            if clarified_drink not in (
                None,
                "",
                "drink",
                "beverage",
            ):
                known_data["drink"] = clarified_drink
                known_data["food_name"] = clarified_drink

        current_missing_field = (
            previous_missing[0]
            if previous_missing
            else None
        )

        if known_data.get("is_combo_meal"):
            lowered_answer = text.lower().strip()

            size_words = {
                "small",
                "medium",
                "large",
            }

            detected_size = clarification.size

            if detected_size in (None, ""):
                for size_word in size_words:
                    if size_word in lowered_answer.split():
                        detected_size = size_word
                        break

            if (
                current_missing_field == "size"
                and detected_size not in (None, "")
            ):
                known_data["size"] = detected_size
                known_data["combo_side_size"] = detected_size
                known_data["combo_drink_size"] = detected_size

            clarified_combo_drink = clarification.drink

            if (
                current_missing_field == "drink"
                and clarified_combo_drink in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                clarified_combo_drink = text.strip()

            if (
                current_missing_field == "drink"
                and clarified_combo_drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarified_combo_drink
                known_data["combo_drink"] = clarified_combo_drink

            if (
                current_missing_field == "size"
                and clarification.drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarification.drink
                known_data["combo_drink"] = clarification.drink

        known_data["_accumulated_text"] = accumulated_text

        candidate_missing = []

        for field in (
            previous_missing
            + list(clarification.missing_fields)
        ):
            if field not in candidate_missing:
                candidate_missing.append(field)

        clarification_priority = (
            "restaurant",
            "brand",
            "size",
            "quantity",
            "quantity_description",
            "drink_detail",
            "drink",
            "meal_category",
            "food_name_detail",
            "food_name",
        )

        unresolved_fields = {
            field
            for field in candidate_missing
            if (
                field in {
                    "food_name_detail",
                    "drink_detail",
                }
                or known_data.get(field) in (None, "")
            )
        }

        if (
            "drink_detail" in unresolved_fields
            and clarification.drink not in (
                None,
                "",
                "drink",
                "beverage",
            )
        ):
            unresolved_fields.remove("drink_detail")

        if (
            "food_name_detail" in unresolved_fields
            and clarification.food_name not in (None, "")
            and normalize_signature_food(
                clarification.food_name
            ) not in {
                "burger",
                "burgers",
                "burrito",
                "burritos",
                "cheeseburger",
                "cheeseburgers",
                "fries",
                "french fries",
                "pizza",
                "sandwich",
                "sandwiches",
                "salad",
                "salads",
                "taco",
                "tacos",
            }
        ):
            unresolved_fields.remove("food_name_detail")

        remaining_missing = [
            field
            for field in clarification_priority
            if field in unresolved_fields
        ]

        remaining_missing.extend(
            field
            for field in candidate_missing
            if (
                field in unresolved_fields
                and field not in remaining_missing
            )
        )

        known_data["missing_fields"] = remaining_missing

        question_by_field = {
            "restaurant": "What restaurant was this from?",
            "brand": "What brand was it?",
            "food_name": "What food did you have?",
            "size": "What size was it?",
            "quantity": "How many did you have?",
            "quantity_description": (
                "How much did you have? Reply with an amount such as "
                "1 serving, 1 cup, 4 oz, or 100 g."
            ),
            "meal_category": "Which meal was this for?",
            "drink": "What drink did you have?",
            "drink_detail": "What drink did you have?",
            "food_name_detail": (
                "Which exact menu item did you have?"
            ),
        }

        if remaining_missing:
            next_field = remaining_missing[0]
            question = question_by_field.get(
                next_field,
                "What detail should I add?",
            )

            known_data["clarification_question"] = question

            update_conversation(
                chat_id=chat_id,
                current_step="clarification",
                known_data=known_data,
                missing_fields=remaining_missing,
            )

            send_telegram_msg(
                question,
                chat_id=chat_id,
            )
            return

        known_data["clarification_question"] = None

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        model_data = {
            field: value
            for field, value in known_data.items()
            if field in FoodInterpretation.model_fields
        }

        interpretation = FoodInterpretation.model_validate(
            model_data
        )

        send_telegram_msg(
            format_food_interpretation(
                interpretation,
                entry_date=known_data.get("_entry_date"),
            ),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "meal_selection"
    ):
        known_data = active_conversation.get("known_data") or {}
        meal_options = (
            known_data.get("_meal_options")
            or food_logging_meal_options(
                known_data.get("_entry_date")
            )
        )

        selected_meal = parse_meal_selection(
            text,
            meal_options,
        )

        if selected_meal is None:
            lines = ["Please choose one of these meals:"]

            for index, option in enumerate(
                meal_options,
                start=1,
            ):
                lines.append(f"{index}. {option.title()}")

            send_telegram_msg(
                "\n".join(lines),
                chat_id=chat_id,
            )
            return

        known_data["meal_category"] = selected_meal
        known_data["missing_fields"] = [
            field
            for field in known_data.get(
                "missing_fields",
                [],
            )
            if field != "meal_category"
        ]
        known_data["clarification_question"] = None

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        interpretation = FoodInterpretation.model_validate(
            known_data
        )

        send_telegram_msg(
            format_food_interpretation(
                interpretation,
                entry_date=known_data.get("_entry_date"),
            ),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "confirmation"
    ):
        lowered = text.lower().strip()

        if lowered in {"1", "correct", "yes"}:
            known_data = active_conversation.get("known_data") or {}

            food_name = known_data.get("food_name")
            size = known_data.get("size")
            brand = known_data.get("brand")
            restaurant = known_data.get("restaurant")

            if known_data.get("is_combo_meal"):
                meal_category = known_data.get(
                    "meal_category"
                )
                quantity = float(
                    known_data.get("quantity") or 1.0
                )
                original_message = (
                    active_conversation.get(
                        "original_message"
                    )
                )

                combo_components = [
                    {
                        "role": "Entrée",
                        "food_name": known_data.get(
                            "combo_entree"
                        ),
                        "size": None,
                    },
                    {
                        "role": "Side",
                        "food_name": known_data.get(
                            "combo_side"
                        ),
                        "size": known_data.get(
                            "combo_side_size"
                        ),
                    },
                    {
                        "role": "Drink",
                        "food_name": known_data.get(
                            "combo_drink"
                        ),
                        "size": known_data.get(
                            "combo_drink_size"
                        ),
                    },
                ]

                missing_components = [
                    component["role"]
                    for component in combo_components
                    if not component["food_name"]
                ]

                if not meal_category:
                    send_telegram_msg(
                        "The combo meal category is missing, "
                        "so nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                if missing_components:
                    send_telegram_msg(
                        "The combo is missing: "
                        + ", ".join(missing_components)
                        + ". Nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                resolved_components = []

                try:
                    for component in combo_components:
                        resolved = (
                            resolve_or_create_verified_food(
                                food_name=component[
                                    "food_name"
                                ],
                                size=component["size"],
                                restaurant=restaurant,
                                brand=None,
                            )
                        )

                        resolved_components.append(
                            {
                                **component,
                                **resolved,
                            }
                        )
                except Exception as error:
                    logging.exception(
                        "Combo nutrition resolution failed"
                    )
                    send_telegram_msg(
                        "I could not verify every component "
                        "of the combo, so nothing was logged.\n\n"
                        f"{error}",
                        chat_id=chat_id,
                    )
                    return

                pending_components = []

                for component in resolved_components:
                    food = component["food"]
                    nutrition = component["nutrition"] or {}

                    pending_components.append(
                        {
                            "role": component["role"],
                            "food_id": food["food_id"],
                            "canonical_name": food[
                                "canonical_name"
                            ],
                            "restaurant": food.get(
                                "restaurant"
                            ),
                            "size": component.get("size"),
                            "quantity": quantity,
                            "calories": nutrition.get(
                                "calories"
                            ),
                            "protein_g": nutrition.get(
                                "protein_g"
                            ),
                            "verification_source": (
                                component.get(
                                    "verification_source"
                                )
                                or food.get(
                                    "verification_source"
                                )
                            ),
                        }
                    )

                update_conversation(
                    chat_id=chat_id,
                    current_step="nutrition_confirmation",
                    known_data={
                        **known_data,
                        "_pending_components": (
                            pending_components
                        ),
                    },
                    missing_fields=[],
                )

                prompt_message_id = send_telegram_msg(
                    format_pending_nutrition_confirmation(
                        pending_components,
                        meal_category=meal_category,
                        entry_date=known_data.get("_entry_date"),
                    ),
                    chat_id=chat_id,
                )

                if isinstance(prompt_message_id, int):
                    update_conversation(
                        chat_id=chat_id,
                        known_data={
                            "_nutrition_prompt_message_id": (
                                prompt_message_id
                            ),
                        },
                    )

                return

            if not food_name:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "I could not identify the food name. "
                    "Please send the food description again.",
                    chat_id=chat_id,
                )
                return

            serving_description = size or "standard"

            try:
                resolution = resolve_food(
                    food_name=food_name,
                    serving_description=serving_description,
                    brand=brand,
                    restaurant=restaurant,
                )
            except Exception:
                logging.exception("Food Library resolution failed")
                send_telegram_msg(
                    "I confirmed the interpretation, but the "
                    "Food Library lookup failed.",
                    chat_id=chat_id,
                )
                return

            if resolution["found"]:
                food = resolution["food"]
                nutrition = resolution["nutrition"] or {}

                meal_category = known_data.get("meal_category")

                if not restaurant:
                    try:
                        quantity = (
                            resolve_non_restaurant_quantity(
                                food_id=food["food_id"],
                                quantity=known_data.get("quantity"),
                                quantity_description=known_data.get(
                                    "quantity_description"
                                ),
                                size=known_data.get("size"),
                                serving_amount=food.get(
                                    "serving_amount"
                                ),
                                serving_unit=food.get(
                                    "serving_unit"
                                ),
                            )
                        )
                    except ValueError as error:
                        quantity_description = str(
                            known_data.get(
                                "quantity_description"
                            )
                            or ""
                        ).lower()

                        if (
                            "handful" in quantity_description
                            and "saved" in str(error).lower()
                            and "amount" in str(error).lower()
                        ):
                            portion_phrase = (
                                "small handful"
                                if "small handful"
                                in quantity_description
                                else (
                                    "large handful"
                                    if "large handful"
                                    in quantity_description
                                    else "handful"
                                )
                            )

                            update_conversation(
                                chat_id=chat_id,
                                current_step=(
                                    "portion_profile_clarification"
                                ),
                                known_data={
                                    **known_data,
                                    "_portion_profile_food_id": (
                                        food["food_id"]
                                    ),
                                    "_portion_profile_phrase": (
                                        portion_phrase
                                    ),
                                },
                                missing_fields=[],
                            )

                            send_telegram_msg(
                                f"I don't know your typical "
                                f"{portion_phrase} for this food yet.\n\n"
                                "About how much is it?\n"
                                "Examples: 28 g, 1 oz, "
                                "1.5 servings.",
                                chat_id=chat_id,
                            )
                            return

                        send_telegram_msg(
                            str(error),
                            chat_id=chat_id,
                        )
                        return
                else:
                    quantity = float(
                        known_data.get("quantity") or 1.0
                    )

                if not meal_category:
                    send_telegram_msg(
                        "The meal category is missing, "
                        "so nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                pending_components = [
                    {
                        "role": "Food",
                        "food_id": food["food_id"],
                        "canonical_name": food[
                            "canonical_name"
                        ],
                        "restaurant": food.get(
                            "restaurant"
                        ),
                        "size": size,
                        "quantity": quantity,
                        "calories": nutrition.get(
                            "calories"
                        ),
                        "protein_g": nutrition.get(
                            "protein_g"
                        ),
                        "verification_source": food.get(
                            "verification_source"
                        ),
                        "matched_by": resolution.get("matched_by"),
                        "requested_food_name": food_name,
                    }
                ]

                update_conversation(
                    chat_id=chat_id,
                    current_step="nutrition_confirmation",
                    known_data={
                        **known_data,
                        "_pending_components": (
                            pending_components
                        ),
                    },
                    missing_fields=[],
                )

                prompt_message_id = send_telegram_msg(
                    format_pending_nutrition_confirmation(
                        pending_components,
                        meal_category=meal_category,
                        entry_date=known_data.get("_entry_date"),
                    ),
                    chat_id=chat_id,
                )

                if isinstance(prompt_message_id, int):
                    update_conversation(
                        chat_id=chat_id,
                        known_data={
                            "_nutrition_prompt_message_id": (
                                prompt_message_id
                            ),
                        },
                    )

                return

            try:
                provider_result = lookup_official_nutrition(
                    restaurant=restaurant,
                    food_name=food_name,
                    size=size,
                    brand=brand,
                )
            except Exception:
                logging.exception(
                    "Official nutrition provider failed"
                )
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Food interpretation confirmed.\n\n"
                    "The food was not found in the Food Library, "
                    "and the official nutrition lookup failed.\n\n"
                    "Send a new food description to try again.",
                    chat_id=chat_id,
                )
                return

            if not provider_result["found"]:
                update_conversation(
                    chat_id=chat_id,
                    current_step="manual_label_offer",
                    known_data={
                        **known_data,
                        "_manual_label_search_key": (
                            resolution["search_key"]
                        ),
                    },
                    missing_fields=[],
                )

                date_label = format_food_entry_date(
                    known_data.get("_entry_date")
                )
                send_telegram_msg(
                    "I couldn't verify this product automatically.\n\n"
                    + (
                        f"Date: {date_label}\n\n"
                        if date_label
                        else ""
                    )
                    + "1. Enter package label nutrition\n"
                    "2. Try a different description\n"
                    "3. Save for later\n"
                    "4. Cancel",
                    chat_id=chat_id,
                )
                return

            provider_food = provider_result["food"]
            provider_nutrition = provider_result["nutrition"]
            verification = provider_result["verification"]

            try:
                saved = add_food_with_nutrition(
                    canonical_name=(
                        provider_food["canonical_name"]
                    ),
                    serving_description=(
                        provider_food["serving_description"]
                    ),
                    serving_amount=(
                        provider_food["serving_amount"]
                    ),
                    serving_unit=(
                        provider_food["serving_unit"]
                    ),
                    verification_status=(
                        verification["status"]
                    ),
                    verification_source=(
                        verification["source"]
                    ),
                    calories=provider_nutrition["calories"],
                    protein_g=provider_nutrition["protein_g"],
                    carbohydrates_g=(
                        provider_nutrition["carbohydrates_g"]
                    ),
                    fat_g=provider_nutrition["fat_g"],
                    fiber_g=provider_nutrition["fiber_g"],
                    sugar_g=provider_nutrition["sugar_g"],
                    sodium_mg=provider_nutrition["sodium_mg"],
                    brand=provider_food["brand"],
                    restaurant=provider_food["restaurant"],
                    food_type=provider_food["food_type"],
                    source_item_id=(
                        verification["source_item_id"]
                    ),
                    source_url=verification["source_url"],
                )
            except Exception:
                logging.exception(
                    "Saving verified Food failed"
                )
                send_telegram_msg(
                    "Verified nutrition was found, but I could "
                    "not save it to the Food Library.",
                    chat_id=chat_id,
                )
                return

            saved_food = saved["food"]
            saved_nutrition = saved["nutrition"] or {}

            meal_category = known_data.get("meal_category")

            if not restaurant:
                try:
                    quantity = (
                        resolve_non_restaurant_quantity(
                            food_id=saved_food["food_id"],
                            quantity=known_data.get("quantity"),
                            quantity_description=known_data.get(
                                "quantity_description"
                            ),
                            size=known_data.get("size"),
                            serving_amount=saved_food.get(
                                "serving_amount"
                            ),
                            serving_unit=saved_food.get(
                                "serving_unit"
                            ),
                        )
                    )
                except ValueError as error:
                    quantity_description = str(
                        known_data.get(
                            "quantity_description"
                        )
                        or ""
                    ).lower()

                    if (
                        "handful" in quantity_description
                        and "saved" in str(error).lower()
                        and "amount" in str(error).lower()
                    ):
                        portion_phrase = (
                            "small handful"
                            if "small handful"
                            in quantity_description
                            else (
                                "large handful"
                                if "large handful"
                                in quantity_description
                                else "handful"
                            )
                        )

                        update_conversation(
                            chat_id=chat_id,
                            current_step=(
                                "portion_profile_clarification"
                            ),
                            known_data={
                                **known_data,
                                "_portion_profile_food_id": (
                                    saved_food["food_id"]
                                ),
                                "_portion_profile_phrase": (
                                    portion_phrase
                                ),
                            },
                            missing_fields=[],
                        )

                        send_telegram_msg(
                            f"I don't know your typical "
                            f"{portion_phrase} for this food yet.\n\n"
                            "About how much is it?\n"
                            "Examples: 28 g, 1 oz, "
                            "1.5 servings.",
                            chat_id=chat_id,
                        )
                        return

                    send_telegram_msg(
                        str(error),
                        chat_id=chat_id,
                    )
                    return
            else:
                quantity = float(
                    known_data.get("quantity") or 1.0
                )

            if not meal_category:
                send_telegram_msg(
                    "The meal category is missing, "
                    "so nothing was logged.",
                    chat_id=chat_id,
                )
                return

            pending_components = [
                {
                    "role": "Food",
                    "food_id": saved_food["food_id"],
                    "canonical_name": saved_food[
                        "canonical_name"
                    ],
                    "restaurant": saved_food.get(
                        "restaurant"
                    ),
                    "size": size,
                    "quantity": quantity,
                    "calories": saved_nutrition.get(
                        "calories"
                    ),
                    "protein_g": saved_nutrition.get(
                        "protein_g"
                    ),
                    "verification_source": verification[
                        "source"
                    ],
                }
            ]

            update_conversation(
                chat_id=chat_id,
                current_step="nutrition_confirmation",
                known_data={
                    **known_data,
                    "_pending_components": pending_components,
                },
                missing_fields=[],
            )

            prompt_message_id = send_telegram_msg(
                format_pending_nutrition_confirmation(
                    pending_components,
                    meal_category=meal_category,
                    entry_date=known_data.get("_entry_date"),
                ),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_nutrition_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        if lowered in {"2", "edit"}:
            prompt_for_corrected_food(
                chat_id=chat_id,
                known_data=known_data,
            )
            return

        if lowered in {"3", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please reply:\n"
            "1. Correct\n"
            "2. Edit\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return

    weight_value = extract_weight_value_from_text(text)
    if weight_value is not None:
        success, response = set_today_weight(weight_value)
        send_telegram_msg(response, chat_id=chat_id)
        return

    sleep_value = extract_sleep_value_from_text(text)
    if sleep_value is not None:
        success, response = set_today_sleep(sleep_value)
        send_telegram_msg(response, chat_id=chat_id)
        return

    if is_sleep_status_question(text):
        send_telegram_msg(answer_sleep_status(), chat_id=chat_id)
        return

    if is_run_weekly_report_command(text):
        send_weekly_report(chat_id=chat_id)
        return

    natural_entry_date, interpretation_text = (
        extract_yesterday_food_intent(text)
    )
    target_entry_date = (
        forced_food_entry_date or natural_entry_date
    )
    if forced_food_entry_date is not None:
        interpretation_text = text

    try:
        interpretation = interpret_food_message(
            interpretation_text
        )

        if interpretation.is_food_logging_request:
            interpretation_data = interpretation.model_dump()
            interpretation_data["_accumulated_text"] = (
                interpretation_text
            )
            if target_entry_date is not None:
                interpretation_data["_entry_date"] = (
                    target_entry_date.isoformat()
                )

            missing_fields = list(
                interpretation.missing_fields
            )

            if missing_fields == ["meal_category"]:
                meal_options = food_logging_meal_options(
                    target_entry_date
                )

                conversation = start_conversation(
                    chat_id=chat_id,
                    conversation_type="food_interpretation",
                    current_step="meal_selection",
                    known_data=interpretation_data,
                    missing_fields=missing_fields,
                    original_message=text,
                )

                conversation["meal_options"] = meal_options

                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_meal_options": meal_options,
                    },
                )

                send_telegram_msg(
                    format_meal_selection_prompt(
                        interpretation,
                        entry_date=target_entry_date,
                    ),
                    chat_id=chat_id,
                )
                return

            if missing_fields:
                start_conversation(
                    chat_id=chat_id,
                    conversation_type="food_interpretation",
                    current_step="clarification",
                    known_data=interpretation_data,
                    missing_fields=missing_fields,
                    original_message=text,
                )

                send_telegram_msg(
                    format_food_interpretation(
                        interpretation,
                        entry_date=target_entry_date,
                    ),
                    chat_id=chat_id,
                )
                return

            # Fast path for trusted, previously saved foods.
            # Keep the normal confirmation screen whenever the
            # interpretation contains assumptions or is a combo.
            if (
                not interpretation.assumptions
                and not interpretation.is_combo_meal
                and interpretation.food_name
                and interpretation.meal_category
            ):
                try:
                    saved_resolution = resolve_food(
                        food_name=interpretation.food_name,
                        serving_description=(
                            interpretation.size or "standard"
                        ),
                        brand=interpretation.brand,
                        restaurant=interpretation.restaurant,
                    )
                except Exception:
                    logging.exception(
                        "Saved-food fast-path resolution failed"
                    )
                    saved_resolution = {"found": False}

                if saved_resolution.get("found"):
                    saved_food = saved_resolution["food"]
                    saved_nutrition = (
                        saved_resolution.get("nutrition") or {}
                    )

                    try:
                        if not interpretation.restaurant:
                            quantity = (
                                resolve_non_restaurant_quantity(
                                    food_id=saved_food["food_id"],
                                    quantity=interpretation.quantity,
                                    quantity_description=(
                                        interpretation.quantity_description
                                    ),
                                    size=interpretation.size,
                                    serving_amount=saved_food.get(
                                        "serving_amount"
                                    ),
                                    serving_unit=saved_food.get(
                                        "serving_unit"
                                    ),
                                )
                            )
                        else:
                            quantity = float(
                                interpretation.quantity or 1.0
                            )
                    except ValueError:
                        quantity = None

                    if quantity is not None:
                        pending_components = [
                            {
                                "role": "Food",
                                "food_id": saved_food["food_id"],
                                "canonical_name": saved_food[
                                    "canonical_name"
                                ],
                                "restaurant": saved_food.get(
                                    "restaurant"
                                ),
                                "size": interpretation.size,
                                "quantity": quantity,
                                "calories": saved_nutrition.get(
                                    "calories"
                                ),
                                "protein_g": saved_nutrition.get(
                                    "protein_g"
                                ),
                                "verification_source": saved_food.get(
                                    "verification_source"
                                ),
                                "matched_by": saved_resolution.get(
                                    "matched_by"
                                ),
                                "requested_food_name": (
                                    interpretation.food_name
                                ),
                            }
                        ]

                        start_conversation(
                            chat_id=chat_id,
                            conversation_type="food_interpretation",
                            current_step="nutrition_confirmation",
                            known_data={
                                **interpretation_data,
                                "_pending_components": (
                                    pending_components
                                ),
                            },
                            missing_fields=[],
                            original_message=text,
                        )

                        prompt_message_id = send_telegram_msg(
                            format_pending_nutrition_confirmation(
                                pending_components,
                                meal_category=(
                                    interpretation.meal_category
                                ),
                                entry_date=target_entry_date,
                            ),
                            chat_id=chat_id,
                        )

                        if isinstance(prompt_message_id, int):
                            update_conversation(
                                chat_id=chat_id,
                                known_data={
                                    "_nutrition_prompt_message_id": (
                                        prompt_message_id
                                    ),
                                },
                            )

                        return

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_interpretation",
                current_step="confirmation",
                known_data=interpretation_data,
                missing_fields=[],
                original_message=text,
            )

            send_telegram_msg(
                format_food_interpretation(
                    interpretation,
                    entry_date=target_entry_date,
                ),
                chat_id=chat_id,
            )
            return
    except Exception:
        logging.exception("Food interpretation failed")

    send_telegram_msg(build_help_message(), chat_id=chat_id)


def process_telegram_update_safely(update):
    """Claim and process one update without allowing duplicate handling."""
    with TELEGRAM_UPDATE_LOCK:
        update_id = update.get("update_id")

        if update_id is not None:
            state = load_state()
            current_offset = state.get("telegram_update_offset")
            if (
                current_offset is not None
                and int(update_id) < int(current_offset)
            ):
                logging.info(
                    "Telegram update %s was already processed; skipping",
                    update_id,
                )
                return

            # Claim the update before conversation state changes. If Telegram
            # repeats the same update, its numeric reply must not cross into
            # the next selection or confirmation step.
            next_offset = int(update_id) + 1
            if current_offset is not None:
                next_offset = max(
                    next_offset,
                    int(current_offset),
                )
            state["telegram_update_offset"] = next_offset
            save_state(state)

        try:
            process_telegram_update(update)
        except Exception:
            logging.exception(
                "Telegram update %s failed and will be skipped",
                update_id,
            )
            chat_id = (
                update.get("message", {})
                .get("chat", {})
                .get("id")
            )
            if chat_id is not None:
                send_telegram_msg(
                    "I couldn't finish processing that message. "
                    "It was skipped so HealthCoach can continue. "
                    "Please send /menu or try again.",
                    chat_id=chat_id,
                )


def telegram_poll_loop():
    logging.info("Telegram polling started")

    while True:
        try:
            state = load_state()
            offset = state.get("telegram_update_offset")

            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = int(offset)

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()

            if not data.get("ok"):
                logging.error("Telegram getUpdates failed: %s", data)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                process_telegram_update_safely(update)

        except Exception as e:
            logging.error("Telegram polling error: %s", e)
            time.sleep(10)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    logging.info("Webhook received: %s", data)

    now = datetime.now(PACIFIC_TZ)
    sheet = get_current_sheet()

    timestamp = now.strftime("%m/%d/%Y %I:%M %p")

    steps = safe_int(data.get("steps"), None)
    total = safe_float(data.get("total_calories"), None)
    active = safe_float(data.get("active_calories"), None)
    sleep_raw = data.get("sleep_hours")
    sleep = normalize_sleep_for_sheet(sleep_raw) if sleep_raw not in ("", None) else ""
    rhr_raw = data.get("rhr")
    rhr_measured_at_raw = data.get("rhr_measured_at")
    rhr, rhr_measured_at = parse_timestamped_health_metric(
        rhr_raw,
        rhr_measured_at_raw,
        expected_date=now.date(),
    )
    weight = safe_float(data.get("weight"), None)
    # Preserve today's existing official weight.
    _, existing_row, _ = get_today_row_index_and_row(
        sheet,
        now.strftime("%m/%d/%Y"),
    )
    if (
        existing_row
        and len(existing_row) > 6
        and existing_row[6] not in ("", None)
    ):
        weight = None

    hrv_raw = data.get("hrv")
    hrv_measured_at_raw = data.get("hrv_measured_at")
    hrv, hrv_measured_at = parse_timestamped_health_metric(
        hrv_raw,
        hrv_measured_at_raw,
        expected_date=now.date(),
        value_parser=parse_positive_float,
    )
    dietary = safe_float(data.get("dietary_calories"), None)
    protein = safe_float(data.get("protein"), None)
    exercise_minutes = safe_float(
        data.get("exercise_minutes"),
        None,
    )
    cardio_fitness_raw = data.get("cardio_fitness")
    cardio_fitness_measured_at_raw = data.get(
        "cardio_fitness_measured_at"
    )
    (
        cardio_fitness,
        cardio_fitness_measured_at,
    ) = parse_timestamped_health_metric(
        cardio_fitness_raw,
        cardio_fitness_measured_at_raw,
        expected_date=now.date(),
    )
    walking_heart_rate_raw = data.get(
        "walking_heart_rate_average"
    )
    parsed_walking_heart_rate_value = parse_walking_heart_rate(
        walking_heart_rate_raw
    )
    walking_heart_rate_measured_at_raw = data.get(
        "walking_heart_rate_measured_at"
    )
    (
        walking_heart_rate,
        walking_heart_rate_measured_at,
    ) = parse_timestamped_health_metric(
        walking_heart_rate_raw,
        walking_heart_rate_measured_at_raw,
        expected_date=now.date(),
        value_parser=parse_walking_heart_rate,
    )
    if (
        walking_heart_rate_raw not in ("", None)
        and parsed_walking_heart_rate_value is None
    ):
        logging.warning(
            "Ignored invalid walking heart-rate value: %r",
            walking_heart_rate_raw,
        )

    timestamped_metrics = (
        (
            "resting heart rate",
            rhr_raw,
            rhr_measured_at_raw,
            rhr,
        ),
        ("HRV", hrv_raw, hrv_measured_at_raw, hrv),
        (
            "Cardio Fitness",
            cardio_fitness_raw,
            cardio_fitness_measured_at_raw,
            cardio_fitness,
        ),
        (
            "walking heart rate",
            walking_heart_rate_raw,
            walking_heart_rate_measured_at_raw,
            walking_heart_rate,
        ),
    )
    for metric_name, raw_value, raw_time, parsed_value in (
        timestamped_metrics
    ):
        if (
            raw_time not in ("", None)
            and raw_value not in ("", None)
            and parsed_value is None
        ):
            logging.warning(
                "Ignored invalid or stale %s reading: value=%r "
                "measured_at=%r",
                metric_name,
                raw_value,
                raw_time,
            )

    blood_pressure_systolic_raw = data.get(
        "blood_pressure_systolic"
    )
    blood_pressure_diastolic_raw = data.get(
        "blood_pressure_diastolic"
    )
    blood_pressure_measured_at_raw = data.get(
        "blood_pressure_measured_at"
    )
    blood_pressure_supplied = any(
        value not in ("", None)
        for value in (
            blood_pressure_systolic_raw,
            blood_pressure_diastolic_raw,
            blood_pressure_measured_at_raw,
        )
    )
    blood_pressure = parse_blood_pressure(
        blood_pressure_systolic_raw,
        blood_pressure_diastolic_raw,
        blood_pressure_measured_at_raw,
        expected_date=now.date(),
    )
    if blood_pressure_supplied and blood_pressure is None:
        logging.warning(
            "Ignored incomplete, invalid, or stale blood-pressure "
            "reading: systolic=%r diastolic=%r measured_at=%r",
            blood_pressure_systolic_raw,
            blood_pressure_diastolic_raw,
            blood_pressure_measured_at_raw,
        )

    clear_existing_columns = set()
    if existing_row:
        padded_existing_row = list(existing_row) + [""] * (
            len(HEADERS) - len(existing_row)
        )
        for (
            _metric_name,
            raw_value,
            raw_time,
            parsed_value,
            value_index,
            measured_at_index,
        ) in (
            (*timestamped_metrics[0], 5, 16),
            (*timestamped_metrics[1], 7, 17),
            (*timestamped_metrics[2], 11, 18),
            (*timestamped_metrics[3], 12, 19),
        ):
            if (
                raw_value not in ("", None)
                and raw_time not in ("", None)
                and parsed_value is None
                and padded_existing_row[measured_at_index]
                in ("", None)
            ):
                clear_existing_columns.update(
                    (value_index, measured_at_index)
                )
        if (
            blood_pressure_supplied
            and blood_pressure is None
            and padded_existing_row[15] in ("", None)
        ):
            clear_existing_columns.update((13, 14, 15))

    row = [
        timestamp,
        steps if steps is not None else "",
        total if total is not None else "",
        active if active is not None else "",
        sleep,
        rhr if rhr is not None else "",
        weight if weight is not None else "",
        hrv if hrv is not None else "",
        dietary if dietary is not None else "",
        protein if protein is not None else "",
        (
            exercise_minutes
            if exercise_minutes is not None
            else ""
        ),
        cardio_fitness if cardio_fitness is not None else "",
        walking_heart_rate if walking_heart_rate is not None else "",
        (
            blood_pressure["systolic"]
            if blood_pressure is not None
            else ""
        ),
        (
            blood_pressure["diastolic"]
            if blood_pressure is not None
            else ""
        ),
        (
            blood_pressure["measured_at"].strftime(
                "%m/%d/%Y %I:%M %p"
            )
            if blood_pressure is not None
            else ""
        ),
        (
            rhr_measured_at.strftime("%m/%d/%Y %I:%M %p")
            if rhr_measured_at is not None
            else ""
        ),
        (
            hrv_measured_at.strftime("%m/%d/%Y %I:%M %p")
            if hrv_measured_at is not None
            else ""
        ),
        (
            cardio_fitness_measured_at.strftime(
                "%m/%d/%Y %I:%M %p"
            )
            if cardio_fitness_measured_at is not None
            else ""
        ),
        (
            walking_heart_rate_measured_at.strftime(
                "%m/%d/%Y %I:%M %p"
            )
            if walking_heart_rate_measured_at is not None
            else ""
        ),
    ]

    update_or_insert_today(
        sheet,
        row,
        now,
        clear_existing_columns=clear_existing_columns,
    )

    return {"status": "ok"}, 200


if __name__ == "__main__":
    os.makedirs("/home/vandal/bots/healthcoach/logs", exist_ok=True)
    os.makedirs("/home/vandal/bots/healthcoach/data", exist_ok=True)

    t1 = threading.Thread(target=telegram_poll_loop, daemon=True)
    t1.start()

    t2 = threading.Thread(target=scheduler_loop, daemon=True)
    t2.start()

    logging.info("HealthCoach server starting")
    app.run(host="0.0.0.0", port=5000)

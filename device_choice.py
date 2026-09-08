from gsheets_cache import call_with_retry, get_spreadsheet

_TAB_NAME = "Buffer file - Device choice"


def _fetch_rows(key_path, sheet_id):
    worksheet = get_spreadsheet(key_path, sheet_id).worksheet(_TAB_NAME)
    return worksheet.get_all_values()


def load_device_choice_rules(key_path, sheet_id):
    """Load the Manufacturing Name/Pro Account Name/Spec/Region/Color ->
    Buffer file choice rules from the "Buffer file - Device choice" tab
    (DRD Buffer Tracking spreadsheet).

    Assembly Rework's FROM sheet needs the *whole device's* display name
    (e.g. "Ajax MotionCam Outdoor (8EU) white (semi-finished product)"),
    not the raw PCB board_name Debug mode uses - this tab is the manually
    curated mapping from WebDB's device/product fields to that exact text,
    matching pcb_choice.py's shape but keyed on more criteria since the
    same product can have several rows differing only by region/color/spec/
    pro-account.
    """
    rows = call_with_retry(_fetch_rows, key_path, sheet_id)[1:]  # skip header
    rules = []
    for row in rows:
        mfg, pro_account, spec, region, color, choice = (row + [""] * 6)[:6]
        mfg, choice = mfg.strip(), choice.strip()
        if not mfg or not choice:
            continue
        rules.append({
            "manufacturing_name": mfg,
            "pro_account_name": pro_account.strip(),
            "spec": spec.strip(),
            "region": region.strip(),
            "color": color.strip(),
            "choice": choice,
        })
    return rules


def format_spec(specific_features) -> str:
    """WebDB's `specific_features` is a list (e.g. [], ["WL"]) - the sheet's
    Spec column stores it in the same bracketed style the site itself
    displays it in ("[]", "[WL]"), so format it the same way for matching."""
    return "[" + ", ".join(specific_features or []) + "]"


def resolve_device_choice(rules, manufacturing_name, pro_account_name, spec, region, color):
    """Match a device's Manufacturing Name/Pro Account Name/Spec/Region/
    Color against the rules and return the matching "Buffer file choice"
    text.

    Each rule field is either a required constraint (must substring-match
    for Manufacturing Name/Pro Account Name, or exact-match for Spec/
    Region/Color) or blank, meaning "don't care" - when several rules
    could apply, the one satisfying the most non-blank constraints wins
    (most specific match), matching pcb_choice.py's region-specific-over-
    universal preference but generalized to more fields.

    Manufacturing Name specificity is compared FIRST, before the optional
    fields - confirmed against a real mismatch: "Ajax MotionCam Outdoor"
    and "Ajax MotionCam Outdoor (PhOD) Jeweller" are both real, distinct
    products in this sheet, and the latter's own name contains the former
    as a substring, so a device that's actually the (PhOD) Jeweller
    variant matches BOTH rows. With every optional field otherwise tied
    (same region/color/spec), scoring by optional-field count alone can't
    tell them apart, so whichever rule happened to sit earlier in the
    sheet won by accident - not because the sheet lacked the right rule
    (it was there all along), but because the match code never gave any
    weight to *how much* of Manufacturing Name actually matched. A longer
    matching Manufacturing Name is always at least as specific as a
    shorter one it contains, so it's compared first, unconditionally.

    Returns None if nothing matches - callers should treat that as "no
    safe value to write" rather than falling back to raw text, since this
    sheet's Board column is a strict validated dropdown and an unmatched
    string would just recreate the "free text in a dropdown cell" bug.
    """
    if not manufacturing_name:
        return None
    pro_account_name = pro_account_name or ""
    region = region or ""
    color = (color or "").strip().lower()

    best_choice = None
    best_specificity = (-1, -1)
    for rule in rules:
        if rule["manufacturing_name"] not in manufacturing_name:
            continue
        if rule["pro_account_name"] and rule["pro_account_name"] not in pro_account_name:
            continue
        if rule["spec"] and rule["spec"] != spec:
            continue
        if rule["region"] and rule["region"] != region:
            continue
        if rule["color"] and rule["color"].lower() != color:
            continue
        optional_field_count = sum(
            1 for field in (rule["pro_account_name"], rule["spec"], rule["region"], rule["color"]) if field
        )
        specificity = (len(rule["manufacturing_name"]), optional_field_count)
        if specificity > best_specificity:
            best_choice = rule["choice"]
            best_specificity = specificity
    return best_choice

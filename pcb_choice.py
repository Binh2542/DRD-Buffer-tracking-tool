from gsheets_cache import call_with_retry, get_spreadsheet

_TAB_NAME = "Buffer file - PCB choice"


def _fetch_rows(key_path, sheet_id):
    worksheet = get_spreadsheet(key_path, sheet_id).worksheet(_TAB_NAME)
    return worksheet.get_all_values()


def load_pcb_choice_rules(key_path, sheet_id):
    """Load the Board Name/Component Version -> Option lookup rules from the
    "Buffer file - PCB choice" tab.

    Each rule is (search_text, regions, option): `search_text` is matched as
    a substring against the raw board_name/component_version the site
    returns; `regions` is the set of region codes the row applies to, or
    None if it applies to every region (blank Region column).
    """
    rows = call_with_retry(_fetch_rows, key_path, sheet_id)[1:]  # skip header row
    rules = []
    for row in rows:
        search_text, region_cell, option = (row + ["", "", ""])[:3]
        search_text = search_text.strip()
        option = option.strip()
        if not search_text or not option:
            continue
        region_cell = region_cell.strip()
        regions = {r.strip() for r in region_cell.split(",") if r.strip()} if region_cell else None
        rules.append((search_text, regions, option))
    return rules


_EXACT_MATCH_PREFIXES = ("EM4", "EM2")


def resolve_board_name(rules, raw_text, region):
    """Match raw_text (board_name/component_version) against the rules and
    return the matching Option, preferring a region-specific match over a
    "applies to every region" one. Falls back to raw_text unchanged if no
    rule matches.

    search_text is normally matched as a substring of raw_text - but for
    the families in _EXACT_MATCH_PREFIXES, it must match EXACTLY instead.
    Substring matching let a shorter, broader rule (e.g.
    "EM4.002.MBR.001v4") silently win over a more specific one for a real,
    distinct board variant that contains it as a prefix (e.g.
    "EM4.002.MBR.001v4[S-SOUND]", which already had its own correct, exact
    rule elsewhere in the sheet - never reached, because the broader
    rule's region matched first and the function returned immediately).
    Found live 2026-11-25 scanning QR 3207e05d561: it resolved to the
    plain variant instead of the Sounder one its own dedicated rule
    specified. Scoped to just these two prefixes (rather than every
    board) since exact matching only works where every rule's search_text
    is confirmed to be the complete, real board_name/component_version
    string - true for EM4/EM2 as of this change, not verified for
    everything else in the sheet. A new, previously-unseen EM4/EM2 suffix
    variant needs its own new exact row added here; it will no longer
    silently inherit a shorter rule's Option the way other families still
    do.
    """
    if not raw_text:
        return raw_text
    exact_only = raw_text.startswith(_EXACT_MATCH_PREFIXES)
    universal_option = None
    for search_text, regions, option in rules:
        matches = raw_text == search_text if exact_only else search_text in raw_text
        if not matches:
            continue
        if regions is None:
            if universal_option is None:
                universal_option = option
        elif region and region in regions:
            return option
    return universal_option or raw_text

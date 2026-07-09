import gspread

_TAB_NAME = "Buffer file - PCB choice"


def _connect(key_path, sheet_id):
    client = gspread.service_account(filename=str(key_path))
    return client.open_by_key(sheet_id)


def load_pcb_choice_rules(key_path, sheet_id):
    """Load the Board Name/Component Version -> Option lookup rules from the
    "Buffer file - PCB choice" tab.

    Each rule is (search_text, regions, option): `search_text` is matched as
    a substring against the raw board_name/component_version the site
    returns; `regions` is the set of region codes the row applies to, or
    None if it applies to every region (blank Region column).
    """
    worksheet = _connect(key_path, sheet_id).worksheet(_TAB_NAME)
    rows = worksheet.get_all_values()[1:]  # skip header row
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


def resolve_board_name(rules, raw_text, region):
    """Match raw_text (board_name/component_version) against the rules and
    return the matching Option, preferring a region-specific match over a
    "applies to every region" one. Falls back to raw_text unchanged if no
    rule matches.
    """
    if not raw_text:
        return raw_text
    universal_option = None
    for search_text, regions, option in rules:
        if search_text not in raw_text:
            continue
        if regions is None:
            if universal_option is None:
                universal_option = option
        elif region and region in regions:
            return option
    return universal_option or raw_text

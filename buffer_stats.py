import datetime as dt


def _parse_date(iso_str):
    if not iso_str:
        return None
    try:
        return dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def compute_daily_buffer_counts(devices, completed_devices, days: int = 90):
    """Return [(date, count), ...] oldest-first for the trailing `days` days.

    A device counts toward a given day if it had already entered the buffer
    by that day and hadn't been completed yet (or was completed on/after
    that day) - reconstructed from import/complete timestamps rather than
    stored daily snapshots, so any historical range is available for free.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=days - 1)

    spans = []
    for device in devices:
        imported = _parse_date(device.get("import_time_iso"))
        if imported:
            spans.append((imported, None))
    for device in completed_devices:
        imported = _parse_date(device.get("import_time_iso"))
        if imported:
            spans.append((imported, _parse_date(device.get("complete_time_iso"))))

    counts = []
    day = start
    while day <= today:
        count = sum(
            1 for imported, completed in spans
            if imported <= day and (completed is None or day <= completed)
        )
        counts.append((day, count))
        day += dt.timedelta(days=1)
    return counts

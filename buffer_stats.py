import datetime as dt


def parse_iso_date(iso_str):
    if not iso_str:
        return None
    try:
        return dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def compute_daily_buffer_counts(devices, completed_devices, days: int = 90):
    """Return [(date, count), ...] oldest-first for the trailing `days` days.

    A device counts toward a given day if it had already entered the buffer
    by that day and had *not yet* completed by the end of that day - this
    matches the actual buffer list's quantity, not a running tally of
    everything that ever passed through. A device imported and completed on
    the same day is already out of the buffer list by then, so it does not
    count toward that day, only the (still-open) days before it. This is
    reconstructed from import/complete timestamps rather than stored daily
    snapshots, so any historical range is available for free.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=days - 1)

    spans = []
    for device in devices:
        imported = parse_iso_date(device.get("import_time_iso"))
        if imported:
            spans.append((imported, None))
    for device in completed_devices:
        imported = parse_iso_date(device.get("import_time_iso"))
        if imported:
            spans.append((imported, parse_iso_date(device.get("complete_time_iso"))))

    counts = []
    day = start
    while day <= today:
        count = sum(
            1 for imported, completed in spans
            if imported <= day and (completed is None or day < completed)
        )
        counts.append((day, count))
        day += dt.timedelta(days=1)
    return counts


def compute_daily_online_repair_counts(online_repairs, days: int = 90):
    """Return [(date, count), ...] oldest-first for the trailing `days` days -
    how many Online Repair events were logged on each day. Unlike the
    buffer's day-end snapshot count, this is a daily activity count (a
    device repaired today counts once, for today, not for every day it's
    been sitting anywhere)."""
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=days - 1)

    repair_dates = [
        d for d in (parse_iso_date(r.get("repair_time_iso")) for r in online_repairs) if d is not None
    ]

    counts = []
    day = start
    while day <= today:
        counts.append((day, sum(1 for d in repair_dates if d == day)))
        day += dt.timedelta(days=1)
    return counts

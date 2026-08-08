"""Presentation helpers shared by every front-end.

NASync has three interfaces over one engine, and anything they each format
themselves drifts: the run summary is already written out in three places and
they disagree about what to report. This module is where a formatter goes so
that doesn't keep happening — a second copy of one of these is a defect, not a
convenience.
"""


def format_bytes(count) -> str:
    """A size for humans: "4.0 KB", "1.2 GB", or "—" for an unknown one.

    ``None`` is a real answer here rather than an error: a directory has no
    meaningful size, and a symlink's recorded size belongs to the link and not
    to its target, so both are reported as unknown instead of as zero.
    """
    if count is None:
        return "—"
    # Up to PB because this is a tool for NAS boxes: stopping at GB printed an
    # 8 TB volume as "8192.0 GB", in the panel whose whole job is reporting it.
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(count)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def format_volume(volume) -> tuple:
    """``(summary, quota_note)`` for a `SAMBAClient.volume_info()` result.

    ``quota_note`` is None unless a quota limits you to less than the disk has
    free — in which case saying only the first line promises room a sync cannot
    use. Shared because it was written out in four places and one of them had
    already dropped the quota line, which is the divergence this module exists
    to stop. Returns ``(None, None)`` when the server doesn't report figures.
    """
    if not volume or not volume.get("total"):
        return None, None
    used = 100 * volume["used"] / volume["total"]
    summary = (f"{format_bytes(volume['free'])} free of "
               f"{format_bytes(volume['total'])} · {used:.0f}% used")
    quota = None
    if volume["disk_free"] > volume["free"]:
        quota = (f"A quota limits you to {format_bytes(volume['free'])} of the "
                 f"{format_bytes(volume['disk_free'])} free on the disk.")
    return summary, quota

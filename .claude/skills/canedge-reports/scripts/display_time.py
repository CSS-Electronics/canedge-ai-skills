"""Show UTC data timestamps in the user's local time (reports, charts, chat). Data and stats stay in UTC.

Time zone order: explicit name (e.g. a --tz argument) > project.json "display_tz" (IANA name, e.g.
"Europe/Berlin") > project.json "display_utc_offset" (legacy fixed offset, e.g. "+02:00") > the PC's
local time zone. IANA names and the PC zone follow daylight saving time; a fixed offset does not.

    import sys; sys.path.insert(0, ".claude/skills/canedge-reports/scripts")
    from display_time import DisplayTime
    dt_ = DisplayTime.from_project(project_cfg, tz=args.tz)
    dt_.fmt("2026-01-15T01:47:02Z")   # '26/01/15 02:47' (Europe/Berlin; YY/MM/DD as on all pages)
    dt_.local(epoch_s)                # naive local datetime, e.g. for matplotlib axes
    dt_.label(first_utc, last_utc)    # 'Europe/Berlin (UTC+01:00 / +02:00)'
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _aware(x) -> dt.datetime:
    """UTC datetime from an ISO string ('Z' or offset), epoch seconds or a datetime (naive = UTC)."""
    if isinstance(x, (int, float)):
        return dt.datetime.fromtimestamp(float(x), dt.timezone.utc)
    if isinstance(x, str):
        x = dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
    return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)


def _offset_str(td: dt.timedelta | None) -> str:
    m = int((td or dt.timedelta()).total_seconds() // 60)
    return f"{'+' if m >= 0 else '-'}{abs(m) // 60:02d}:{abs(m) % 60:02d}"


class DisplayTime:
    def __init__(self, tz: str | None = None, utc_offset: str | None = None):
        self.tz: dt.tzinfo | None = None  # None = the PC's local time zone
        self.name = "PC local time"
        if tz:
            try:
                self.tz, self.name = ZoneInfo(tz), tz
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise SystemExit(f"ERROR: Unknown time zone '{tz}' - use an IANA name, e.g. Europe/Berlin") from e
        elif utc_offset:
            sign = -1 if utc_offset.startswith("-") else 1
            hh, mm = (utc_offset.lstrip("+-").split(":") + ["0"])[:2]
            self.tz = dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))
            self.name = f"UTC{_offset_str(self.tz.utcoffset(None))}"

    @classmethod
    def from_project(cls, project_cfg: dict | None, tz: str | None = None) -> "DisplayTime":
        p = project_cfg or {}
        return cls(tz or p.get("display_tz"), None if tz or p.get("display_tz") else p.get("display_utc_offset"))

    def page_tz(self) -> str | None:
        """The zone for a browser page (dashboard kit CFG.tz): An IANA name, a fixed offset in whole hours as
        Etc/GMT-N, or None = the viewer's PC time zone."""
        if self.tz is None or isinstance(self.tz, ZoneInfo):
            return None if self.tz is None else self.name
        m = int(self.tz.utcoffset(None).total_seconds() // 60)
        return None if m % 60 else ("UTC" if m == 0 else f"Etc/GMT{-m // 60:+d}")  # Etc/GMT signs are inverted

    def aware(self, x) -> dt.datetime:
        u = _aware(x)
        return u.astimezone(self.tz) if self.tz else u.astimezone()

    def local(self, x) -> dt.datetime:
        """Naive local datetime (plot it with a UTC formatter so matplotlib does not shift it again)."""
        return self.aware(x).replace(tzinfo=None)

    def fmt(self, x, f: str = "%y/%m/%d %H:%M") -> str:
        return self.aware(x).strftime(f)

    def epoch(self, local_naive: dt.datetime) -> float:
        """Epoch seconds of a naive local datetime (e.g. local midnights for axis ticks)."""
        return local_naive.replace(tzinfo=self.tz).timestamp() if self.tz else local_naive.timestamp()

    def label(self, *times) -> str:
        """Zone name plus the UTC offset(s) in effect at the given times."""
        offs = sorted({_offset_str(self.aware(t).utcoffset()) for t in times} or {_offset_str(self.aware(0).utcoffset())})
        if self.name.startswith("UTC"):
            return self.name
        return f"{self.name} (UTC{' / '.join(offs)})"

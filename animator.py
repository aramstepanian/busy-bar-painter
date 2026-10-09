#!/usr/bin/env python3
"""Busy Bar 72x16 sky painter (iss-scene).

Period follows solar elevation for the configured place (Santa Clara by default).
Clock is 12-hour numbers only (``2:08``), native small-font overlay (ids city, temp).
XPM id is ``frame`` (align top_left). No z_index or width. The bar rejects those.
The sky is the original animated painter: drifting clouds, a moving sun,
weather, and night stars only after sunset. Tight glyph beds keep the clock
readable. Each frame is posted again with the same element ids, so the bar
overwrites the picture in place and the screen is never cleared between frames.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Device / place
# ---------------------------------------------------------------------------

W, H = 72, 16
DEFAULT_LAT = 37.3541
DEFAULT_LON = -121.9552
DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_LABEL = "Santa Clara, CA"
DEFAULT_HOST = "http://10.0.4.20"
CLOUD_DRAW_URL = "https://api.busy.app/busybar/display/draw"
APP_NAME = "iss-scene"
PRIORITY = 70
# Desktop Chrome. The cloud API sits behind Cloudflare, which rejects a
# default Python user agent. USB does not need this string.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEALTH_HOST, HEALTH_PORT = "127.0.0.1", 8765
PREVIEW_HOST, PREVIEW_PORT = "127.0.0.1", 43123

HOME_LAT = DEFAULT_LAT
HOME_LON = DEFAULT_LON
TZ = ZoneInfo(DEFAULT_TZ)
LABEL = DEFAULT_LABEL

ISS_NEAR_KM = 1500.0
ISS_OVERHEAD_KM = 700.0
ISS_ALT_FALLBACK_KM = 420.0
EARTH_R_KM = 6371.0

MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
SYNODIC_DAYS = 29.530588853

def weather_url() -> str:
    """Open-Meteo forecast for the place configured at startup."""
    tz_name = getattr(TZ, "key", None) or DEFAULT_TZ
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={HOME_LAT}&longitude={HOME_LON}"
        "&current=temperature_2m,weather_code,cloud_cover,visibility"
        "&daily=sunrise,sunset"
        f"&timezone={quote(tz_name, safe='')}"
    )
ISS_URLS = (
    "https://api.wheretheiss.at/v1/satellites/25544",
    "http://api.open-notify.org/iss-now.json",
)

PERIODS = ("morning", "day", "evening", "night")
WEATHERS = ("clear", "partly", "cloudy", "fog", "rain", "snow", "storm")

# Overlay is always lamp. Ink-on-cyan vanished on the front LED (3:09 PM photo).
# Tight deep-blue beds sit under the glyphs only — not a left-half navy plate.
INK = "#1A1008FF"
LAMP = "#FFE7B0FF"
INK_RGB = (0x1A, 0x10, 0x08)
LAMP_RGB = (0xFF, 0xE7, 0xB0)
BED_RGB = (0x1C, 0x38, 0x68)  # deep sky, same family as day blue

TEXT_X = 20
TIME_Y = 5
TEMP_Y = 11
PARK_Y = -30

log = logging.getLogger("iss-scene")

# ---------------------------------------------------------------------------
# Small fixed palette. Every pixel is quantized here so a stray glyph
# (the overnight KeyError on 'S') cannot appear in a frame.
# chars are 1-byte and never include space (XPM2 color lines split on space).
# ---------------------------------------------------------------------------

_PALETTE_RGB = (
    (0x08, 0x0C, 0x14),  # .  night deep
    (0x12, 0x20, 0x3A),  # #  night mid
    (0x1C, 0x38, 0x68),  # *  night blue
    (0xFF, 0xD4, 0xA0),  # a  star warm
    (0xE8, 0xEE, 0xFF),  # b  star cool
    (0xF0, 0xE6, 0xC8),  # c  moon lit
    (0x3A, 0x48, 0x60),  # d  earthshine
    (0x7A, 0x28, 0x10),  # e  dawn deep
    (0xE0, 0x58, 0x1C),  # f  dawn orange
    (0xF0, 0x90, 0x48),  # g  dawn peach
    (0x4A, 0x6E, 0x98),  # h  dawn blue (muted, not pale-white)
    (0xFF, 0xE0, 0x70),  # i  sun
    (0x2A, 0x78, 0xC8),  # j  day sky
    (0x68, 0xB0, 0xE8),  # k  day light
    (0x14, 0x50, 0x90),  # l  day deep
    (0x48, 0x10, 0x22),  # m  dusk deep
    (0xC8, 0x38, 0x20),  # n  dusk red
    (0xF0, 0x70, 0x30),  # o  dusk orange
    (0x58, 0x68, 0x78),  # p  fog dark
    (0x8A, 0x98, 0xA4),  # q  fog mid
    (0xB8, 0xC0, 0xC8),  # r  fog light  (still below white text)
    (0x16, 0x24, 0x34),  # s  rain dark
    (0x28, 0x44, 0x60),  # t  rain mid
    (0x60, 0x98, 0xC0),  # u  rain streak / cool cloud
    (0xC8, 0xD0, 0xD4),  # v  cloud
    (0x88, 0x90, 0x98),  # w  cloud dark
    (0xE4, 0xEC, 0xF2),  # x  snow
    (0x1C, 0x14, 0x28),  # y  storm
    (0xF8, 0xE8, 0x70),  # z  lightning
    (0xC0, 0xC8, 0xD0),  # A  ISS body
    (0xE8, 0x50, 0x40),  # B  ISS accent
    (0x0C, 0x10, 0x18),  # C  scrim
)

_PALETTE_CHARS = ".#*abcdefghijklmnopqrstuvwxyzABC"
assert len(_PALETTE_RGB) == 32
assert len(_PALETTE_CHARS) == 32
assert "S" not in _PALETTE_CHARS  # the overnight freeze used an unmapped S
assert " " not in _PALETTE_CHARS

CHAR_TO_RGB = dict(zip(_PALETTE_CHARS, _PALETTE_RGB))
RGB_TO_CHAR = {rgb: ch for ch, rgb in CHAR_TO_RGB.items()}
PALETTE_HEX = {
    ch: f"#{r:02X}{g:02X}{b:02X}" for ch, (r, g, b) in CHAR_TO_RGB.items()
}


def nearest_palette(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    best = _PALETTE_RGB[0]
    best_d = 10**9
    for pr, pg, pb in _PALETTE_RGB:
        d = (r - pr) * (r - pr) + (g - pg) * (g - pg) + (b - pb) * (b - pb)
        if d < best_d:
            best_d = d
            best = (pr, pg, pb)
            if d == 0:
                break
    return best


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def hex_rgb(s: str) -> tuple[int, int, int]:
    s = s[1:] if s.startswith("#") else s
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


# ---------------------------------------------------------------------------
# Canvas + XPM2
# ---------------------------------------------------------------------------

class Canvas:
    def __init__(self, fill: tuple[int, int, int] = (8, 12, 20)) -> None:
        self.px = [[fill for _ in range(W)] for _ in range(H)]

    def get(self, x: int, y: int) -> tuple[int, int, int]:
        if 0 <= x < W and 0 <= y < H:
            return self.px[y][x]
        return (0, 0, 0)

    def set(self, x: int, y: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.px[y][x] = nearest_palette(rgb)

    def blend(self, x: int, y: int, rgb: tuple[int, int, int], a: float) -> None:
        if not (0 <= x < W and 0 <= y < H):
            return
        self.set(x, y, mix(self.px[y][x], rgb, a))

    def fill(self, rgb: tuple[int, int, int]) -> None:
        q = nearest_palette(rgb)
        for y in range(H):
            for x in range(W):
                self.px[y][x] = q

    def sample_avg(self, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int]:
        n = 0
        rs = gs = bs = 0
        for y in range(max(0, y0), min(H, y1 + 1)):
            for x in range(max(0, x0), min(W, x1 + 1)):
                r, g, b = self.px[y][x]
                rs += r
                gs += g
                bs += b
                n += 1
        if not n:
            return (0, 0, 0)
        return rs // n, gs // n, bs // n

    def to_xpm(self) -> str:
        """XPM2, #RRGGBB only, every glyph present in the color table."""
        used: dict[str, tuple[int, int, int]] = {}
        rows: list[str] = []
        fallback = _PALETTE_CHARS[0]
        for y in range(H):
            chars: list[str] = []
            for x in range(W):
                rgb = nearest_palette(self.px[y][x])
                ch = RGB_TO_CHAR.get(rgb)
                if ch is None:
                    rgb = nearest_palette(rgb)
                    ch = RGB_TO_CHAR.get(rgb, fallback)
                used[ch] = rgb
                chars.append(ch)
            rows.append("".join(chars))
        if not used:
            used[fallback] = CHAR_TO_RGB[fallback]
        if len(used) > 32:
            raise RuntimeError(f"XPM palette too large: {len(used)}")
        for row in rows:
            for ch in row:
                if ch not in used:
                    raise KeyError(ch)
        color_lines = []
        for ch, rgb in used.items():
            color_lines.append(f"{ch} c {PALETTE_HEX[ch]}")
        header = f"{W} {H} {len(used)} 1"
        return "\n".join(["! XPM2", header, *color_lines, *rows])


# ---------------------------------------------------------------------------
# Tiny 3x5 glyphs — used only to composite preview PNGs (bar uses font=small)
# ---------------------------------------------------------------------------

_GLYPHS = {
    "0": ("0110", "1001", "1001", "1001", "0110"),
    "1": ("0010", "0110", "0010", "0010", "0111"),
    "2": ("1110", "0001", "0110", "1000", "1111"),
    "3": ("1110", "0001", "0110", "0001", "1110"),
    "4": ("1001", "1001", "1111", "0001", "0001"),
    "5": ("1111", "1000", "1110", "0001", "1110"),
    "6": ("0110", "1000", "1110", "1001", "0110"),
    "7": ("1111", "0001", "0010", "0100", "0100"),
    "8": ("0110", "1001", "0110", "1001", "0110"),
    "9": ("0110", "1001", "0111", "0001", "0110"),
    ":": ("000", "010", "000", "010", "000"),
    "A": ("0110", "1001", "1111", "1001", "1001"),
    "P": ("1110", "1001", "1110", "1000", "1000"),
    "M": ("1001", "1111", "1001", "1001", "1001"),
    "C": ("0110", "1001", "1000", "1001", "0110"),
    "-": ("0000", "0000", "1110", "0000", "0000"),
    " ": ("0000", "0000", "0000", "0000", "0000"),
}


def text_size(s: str) -> tuple[int, int]:
    w = 0
    for ch in s:
        g = _GLYPHS.get(ch, _GLYPHS[" "])
        w += len(g[0]) + 1
    return (max(0, w - 1), 5)


def _stamp_text(canvas: Canvas, s: str, x0: int, y0: int, rgb: tuple[int, int, int]) -> None:
    x = x0
    for ch in s:
        g = _GLYPHS.get(ch, _GLYPHS[" "])
        for gy, row in enumerate(g):
            for gx, bit in enumerate(row):
                if bit == "1":
                    canvas.set(x + gx, y0 + gy, rgb)
        x += len(g[0]) + 1


def blit_text(
    canvas: Canvas,
    s: str,
    cx: int,
    cy: int,
    rgb: tuple[int, int, int],
    shadow: tuple[int, int, int] | None = None,
) -> None:
    tw, th = text_size(s)
    x0 = cx - tw // 2
    y0 = cy - th // 2
    if shadow is not None:
        _stamp_text(canvas, s, x0 + 1, y0 + 1, shadow)
    _stamp_text(canvas, s, x0, y0, rgb)


# ---------------------------------------------------------------------------
# Astronomy
# ---------------------------------------------------------------------------

def _julian(dt: datetime) -> float:
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    day = dt.day + (dt.hour + (dt.minute + dt.second / 60.0) / 60.0) / 24.0
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5


def solar_position(
    dt: datetime, lat: float | None = None, lon: float | None = None
) -> tuple[float, float]:
    """Return (azimuth degrees clockwise from north, elevation degrees)."""
    if lat is None:
        lat = HOME_LAT
    if lon is None:
        lon = HOME_LON
    jd = _julian(dt)
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    ep = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(ep) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(ep) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    lst = math.radians(((gmst + lon / 15.0) % 24.0) * 15.0)
    ha = lst - ra
    lat_r = math.radians(lat)
    el = math.asin(
        math.sin(lat_r) * math.sin(dec) + math.cos(lat_r) * math.cos(dec) * math.cos(ha)
    )
    az = math.atan2(
        -math.cos(dec) * math.sin(ha),
        math.cos(lat_r) * math.sin(dec) - math.sin(lat_r) * math.cos(dec) * math.cos(ha),
    )
    return (math.degrees(az) % 360.0, math.degrees(el))


def _solar_noon_utc(day_local: datetime) -> datetime:
    """Rough solar noon in UTC for the local calendar day."""
    local_noon = datetime(day_local.year, day_local.month, day_local.day, 12, 0, tzinfo=TZ)
    # iterate elevation max
    best = local_noon.astimezone(timezone.utc)
    best_el = -90.0
    for minutes in range(-180, 181, 2):
        t = local_noon + timedelta(minutes=minutes)
        _, el = solar_position(t)
        if el > best_el:
            best_el = el
            best = t.astimezone(timezone.utc)
    return best


_SUN_TIMES: dict[tuple[int, int, int], tuple[datetime, datetime]] = {}


def sunrise_sunset(day_local: datetime) -> tuple[datetime, datetime]:
    """Civil geometric sunrise/sunset (sun elevation = -0.833 deg)."""
    local = day_local.astimezone(TZ)
    key = (local.year, local.month, local.day)
    cached = _SUN_TIMES.get(key)
    if cached:
        return cached
    noon = _solar_noon_utc(local)
    start = noon - timedelta(hours=8)

    def crossing(before: datetime, after: datetime, rising: bool) -> datetime:
        lo, hi = before, after
        for _ in range(24):
            mid = lo + (hi - lo) / 2
            _, el = solar_position(mid)
            if rising:
                if el < -0.833:
                    lo = mid
                else:
                    hi = mid
            else:
                if el > -0.833:
                    lo = mid
                else:
                    hi = mid
        return hi

    rise = set_ = noon
    prev = start
    _, prev_el = solar_position(prev)
    t = start
    found_r = found_s = False
    while t <= noon + timedelta(hours=8):
        t += timedelta(minutes=4)
        _, el = solar_position(t)
        if not found_r and prev_el < -0.833 <= el:
            rise = crossing(prev, t, True)
            found_r = True
        if not found_s and prev_el > -0.833 >= el:
            set_ = crossing(prev, t, False)
            found_s = True
        prev, prev_el = t, el
    times = (rise.astimezone(TZ), set_.astimezone(TZ))
    _SUN_TIMES[key] = times
    return times


def period_for(dt: datetime, rise: datetime | None = None, sett: datetime | None = None) -> str:
    """Sky period from Santa Clara solar geometry, not a wall clock and not UTC.

    Open-Meteo daily sunrise/sunset are local wall times. Parsing them with
    ``datetime.fromisoformat(s).astimezone(TZ)`` on a UTC host treats 19:25 as
    19:25 UTC = 12:25 PM PT, so 10:27 AM became evening and 1:31 PM became
    night. Period always uses the ephemeris (sun elevation / our rise-set),
    never those API strings.
    """
    local = dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    _, el = solar_position(local)
    if el < -0.833:
        return "night"
    try:
        rise, sett = sunrise_sunset(local)
    except Exception:
        rise = sett = None
    if rise is not None and sett is not None:
        if rise <= local < rise + timedelta(hours=2):
            return "morning"
        if sett - timedelta(hours=2) <= local < sett:
            return "evening"
        return "day"
    if el >= 20:
        return "day"
    az, _ = solar_position(local)
    return "morning" if az < 180 else "evening"


def moon_phase(dt: datetime) -> float:
    """0 new, 0.5 full. Waxing toward 0.5, waning after."""
    age = (dt.astimezone(timezone.utc) - MOON_EPOCH).total_seconds() / 86400.0
    return (age / SYNODIC_DAYS) % 1.0


def sidereal_angle(dt: datetime) -> float:
    jd = _julian(dt)
    n = jd - 2451545.0
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    lst = (gmst + HOME_LON / 15.0) % 24.0
    return lst / 24.0 * 2.0 * math.pi


# ---------------------------------------------------------------------------
# Weather + ISS
# ---------------------------------------------------------------------------

def http_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": CHROME_UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def classify_weather(code: int, visibility_m: float | None = None) -> str:
    if visibility_m is not None and visibility_m < 1200 and code in (0, 1, 2, 3, 45, 48):
        return "fog"
    if code in (45, 48):
        return "fog"
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code == 3:
        return "cloudy"
    if code == 2:
        return "partly"
    return "clear"


def parse_local_iso(value: object) -> datetime | None:
    """Parse Open-Meteo local timestamps. Naive strings are Santa Clara wall time."""
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    t = datetime.fromisoformat(raw)
    if t.tzinfo is None:
        return t.replace(tzinfo=TZ)
    return t.astimezone(TZ)


def fetch_weather() -> dict:
    data = http_json(weather_url())
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    code = int(cur.get("weather_code") or 0)
    vis = cur.get("visibility")
    temp = cur.get("temperature_2m")
    rise_s = (daily.get("sunrise") or [None])[0]
    set_s = (daily.get("sunset") or [None])[0]
    return {
        "temp_c": None if temp is None else int(round(float(temp))),
        "weather": classify_weather(code, None if vis is None else float(vis)),
        "code": code,
        "sunrise": parse_local_iso(rise_s),
        "sunset": parse_local_iso(set_s),
        "cloud": cur.get("cloud_cover"),
    }


def _ecef(lat: float, lon: float, alt_km: float) -> tuple[float, float, float]:
    r = EARTH_R_KM + alt_km
    la, lo = math.radians(lat), math.radians(lon)
    return r * math.cos(la) * math.cos(lo), r * math.cos(la) * math.sin(lo), r * math.sin(la)


def iss_distance_km(lat: float, lon: float, alt_km: float) -> float:
    ax, ay, az = _ecef(HOME_LAT, HOME_LON, 0.0)
    bx, by, bz = _ecef(lat, lon, alt_km)
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)


def iss_bearing_east_km(lat: float, lon: float) -> tuple[float, float]:
    """Approximate east/north ground offset in km (for sliding)."""
    dlat = math.radians(lat - HOME_LAT)
    dlon = math.radians(lon - HOME_LON)
    north = dlat * EARTH_R_KM
    east = dlon * EARTH_R_KM * math.cos(math.radians(HOME_LAT))
    return east, north


def fetch_iss() -> dict | None:
    last_err: Exception | None = None
    for url in ISS_URLS:
        try:
            data = http_json(url, timeout=6.0)
            if "latitude" in data and "longitude" in data:
                lat = float(data["latitude"])
                lon = float(data["longitude"])
                alt = float(data.get("altitude") or ISS_ALT_FALLBACK_KM)
            else:
                pos = data.get("iss_position") or {}
                lat = float(pos["latitude"])
                lon = float(pos["longitude"])
                alt = ISS_ALT_FALLBACK_KM
            dist = iss_distance_km(lat, lon, alt)
            east, north = iss_bearing_east_km(lat, lon)
            if dist < ISS_OVERHEAD_KM:
                state = "overhead"
            elif dist < ISS_NEAR_KM:
                state = "near"
            else:
                state = "far"
            return {
                "lat": lat,
                "lon": lon,
                "alt_km": alt,
                "dist_km": dist,
                "east_km": east,
                "north_km": north,
                "state": state,
            }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    if last_err:
        log.warning("ISS poll failed: %s", last_err)
    return None


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------

# Classic dipper + W, in px relative to the celestial pole.
_DIPPER = ((-7, -1), (-5, 1), (-2, 2), (1, 0), (3, -1), (6, -2), (9, 0))
_CAS = ((-6, 2), (-3, -2), (0, 1), (3, -2), (6, 2))
# Fixed field stars (warm→cool). Positions are pre-rotation; they do not blink.
# Field stars stay out of the clock/temp bed (x 6–34, y 2–13).
_FIELD = (
    (4, 0, 0), (12, 1, 1), (20, 0, 2), (28, 1, 0), (35, 0, 1),
    (42, 6, 2), (46, 2, 0), (51, 9, 1), (57, 4, 2), (63, 8, 0),
    (67, 3, 1), (8, 15, 2), (16, 14, 0), (48, 13, 1), (60, 12, 2),
    (2, 14, 1), (24, 15, 0), (70, 14, 2), (41, 10, 0), (43, 1, 1),
)
_STAR_RGB = ((255, 212, 160), (255, 236, 210), (200, 220, 255))


def _rot(px: float, py: float, ang: float) -> tuple[float, float]:
    c, s = math.cos(ang), math.sin(ang)
    return px * c - py * s, px * s + py * c


def _sun_xy(period: str, dt: datetime | None = None) -> tuple[int, int, int]:
    """Sun stays on the right so it never sits under the clock.

    When ``dt`` is given, x/y follow Santa Clara solar azimuth and elevation
    so the disk actually travels (low east at dawn, high at noon, low west
    at dusk) instead of three frozen period buckets.
    """
    if dt is not None:
        az, el = solar_position(dt)
        if el >= -0.833:
            y = int(round(13.0 - max(0.0, min(70.0, el)) / 70.0 * 11.0))
            y = max(2, min(13, y))
            if az < 180:
                t = max(0.0, min(1.0, (az - 80.0) / 100.0))
                x = int(round(66 - t * 12))
            else:
                t = max(0.0, min(1.0, (az - 180.0) / 90.0))
                x = int(round(54 + t * 13))
            x = max(50, min(68, x))
            rad = 4 if el < 25 else 3
            return x, y, rad
    if period == "morning":
        return 62, 12, 4
    if period == "day":
        return 58, 4, 3
    if period == "evening":
        return 64, 11, 4
    return 58, 8, 3


def _period_stops(period: str) -> list[tuple[int, int, int]]:
    """Vertical sky colors, top → bottom. Morning is not a pale wash."""
    if period == "morning":
        return [(52, 86, 130), (240, 130, 64), (224, 80, 28), (122, 40, 16)]
    if period == "day":
        return [(104, 176, 232), (42, 120, 200), (20, 80, 144)]
    if period == "evening":
        return [(48, 32, 72), (200, 56, 40), (240, 112, 48), (90, 20, 28)]
    return [(8, 12, 20), (18, 32, 58), (12, 22, 40)]


def paint_base_sky(
    c: Canvas,
    period: str,
    weather: str,
    dt: datetime | None = None,
    tsec: float = 0.0,
) -> None:
    stops = _period_stops(period)
    sun_x, sun_y, _ = _sun_xy(period, dt)
    for y in range(H):
        ty = y / (H - 1)
        seg = ty * (len(stops) - 1)
        i = min(int(seg), len(stops) - 2)
        local = seg - i
        row = mix(stops[i], stops[i + 1], local)
        for x in range(W):
            rgb = row
            if period in ("morning", "evening") and weather in ("clear", "partly"):
                # Pull the right side toward the sun so it is not 3 flat bands.
                dx = (x - sun_x) / 28.0
                dy = (y - sun_y) / 10.0
                glow = math.exp(-(dx * dx + dy * dy))
                sun_col = (255, 224, 112) if period == "morning" else (255, 140, 64)
                rgb = mix(rgb, sun_col, 0.45 * glow)
            if period == "day" and weather in ("clear", "partly"):
                dx = (x - sun_x) / 22.0
                dy = (y - sun_y) / 9.0
                glow = math.exp(-(dx * dx + dy * dy))
                rgb = mix(rgb, (255, 244, 180), 0.28 * glow)
                rgb = mix(rgb, (255, 240, 160), 0.10 * (1.0 - y / 15.0) * (x / 71.0))
            c.set(x, y, rgb)

    if weather in ("cloudy", "rain", "storm"):
        # Weather changes the sky, not a sticker on top of a postcard.
        for y in range(H):
            for x in range(W):
                cool = (30, 48, 68) if weather != "storm" else (28, 20, 40)
                amt = 0.55 if weather == "cloudy" else 0.72
                c.set(x, y, mix(c.get(x, y), cool, amt))
    if weather == "fog":
        paint_fog(c, period, dt, tsec)
    if weather == "snow" and period != "night":
        for y in range(H):
            for x in range(W):
                c.set(x, y, mix(c.get(x, y), (160, 176, 188), 0.45))


def paint_fog(c: Canvas, period: str, dt: datetime | None = None, tsec: float = 0.0) -> None:
    """South Bay marine layer — a first-class scene, not a missing branch."""
    warm = period in ("morning", "evening")
    for y in range(H):
        t = y / (H - 1)
        if warm:
            top, bot = (110, 128, 142), (168, 164, 154)
        else:
            top, bot = (88, 102, 116), (176, 184, 190)
        row = mix(top, bot, t)
        for x in range(W):
            wisp = 0.08 * math.sin(x * 0.35 + y * 0.8 + tsec * 0.45) + 0.06 * math.sin(
                x * 0.11 + 2 + tsec * 0.2
            )
            layer = 0.15 if y in (4, 5, 9, 10, 13) else 0.0
            rgb = mix(row, (196, 202, 206), max(0.0, wisp + layer))
            c.set(x, y, rgb)
    if period in ("morning", "day", "evening"):
        sx, sy, _ = _sun_xy(period, dt)
        for y in range(H):
            for x in range(W):
                d = math.hypot(x - sx, y - sy)
                if d < 5:
                    c.blend(x, y, (210, 200, 170), 0.18 * (1.0 - d / 5.0))


def paint_clouds(c: Canvas, weather: str, tsec: float) -> None:
    """Drifting cloud deck. Clear days still get thin cirrus so the bar is not a still."""
    if weather == "fog":
        return
    dark = weather in ("rain", "storm", "cloudy")
    col = (70, 82, 96) if dark else (200, 208, 214)
    col2 = (50, 60, 72) if dark else (136, 144, 152)
    blobs = (
        (10, 3, 9, 3),
        (28, 2, 8, 2),
        (48, 4, 10, 3),
        (64, 3, 7, 2),
        (18, 6, 7, 2),
        (40, 7, 9, 3),
        (6, 8, 8, 2),
        (55, 6, 11, 3),
    )
    if weather == "clear":
        blobs = ((16, 2, 14, 1.6), (42, 3, 16, 1.4), (60, 5, 11, 1.5))
        col, col2 = (214, 222, 230), (186, 198, 210)
    elif weather == "partly":
        blobs = blobs[:4]
    elif weather == "snow":
        blobs = blobs[:6]
    wrap = W + 18
    drift = (tsec * 1.55) % wrap
    drift2 = (tsec * 0.72 + 11) % wrap
    for i, (bx, by, rx, ry) in enumerate(blobs):
        d = drift if i % 2 == 0 else drift2
        bob = 0.7 * math.sin(tsec * 0.18 + i * 1.3)
        cx = (bx + d) % wrap - 8
        cy = by + bob
        for y in range(H):
            for x in range(W):
                dx = (x - cx) / rx
                dy = (y - cy) / ry
                if dx * dx + dy * dy <= 1.0:
                    if weather == "clear":
                        c.blend(x, y, col2 if y > cy else col, 0.42)
                    else:
                        c.set(x, y, col2 if y > cy else col)


def paint_sun(c: Canvas, period: str, weather: str, dt: datetime | None = None) -> None:
    if period == "night":
        return
    if weather in ("fog", "storm", "rain", "cloudy"):
        return
    sx, sy, rad = _sun_xy(period, dt)
    core = (255, 224, 112)
    glow = (240, 144, 48) if period != "day" else (255, 236, 160)
    for y in range(H):
        for x in range(W):
            d = math.hypot(x - sx, y - sy)
            if d <= rad * 0.55:
                c.set(x, y, core)
            elif d <= rad:
                c.set(x, y, mix(core, glow, (d - rad * 0.55) / (rad * 0.45)))
            elif d <= rad + 3:
                c.blend(x, y, glow, 0.4 * (1.0 - (d - rad) / 3.0))


def paint_stars_and_moon(c: Canvas, dt: datetime, weather: str) -> None:
    if weather in ("fog", "storm"):
        if weather == "storm":
            return
        c.set(50, 1, (232, 238, 255))
        c.set(61, 2, (255, 212, 160))
        return
    ang = sidereal_angle(dt)
    pole = (56.0, 6.5)
    dim = weather in ("cloudy", "rain", "snow")
    for x, y, kind in _FIELD:
        if dim and kind == 0:
            continue
        c.set(x, y, _STAR_RGB[kind if not dim else 2])

    def plot_constellation(points: Iterable[tuple[int, int]], phase: float) -> None:
        for px, py in points:
            rx, ry = _rot(px, py, ang + phase)
            x = int(round(pole[0] + rx))
            y = int(round(pole[1] + ry))
            warm = rx < 0
            rgb = (255, 212, 160) if warm else (200, 220, 255)
            if dim:
                rgb = mix(rgb, (28, 40, 70), 0.4)
            c.set(x, y, rgb)

    plot_constellation(_DIPPER, 0.0)
    plot_constellation(_CAS, math.pi)

    if weather in ("rain", "cloudy"):
        return
    paint_moon(c, moon_phase(dt), 64, 5, 3)


def paint_moon(c: Canvas, phase: float, cx: int, cy: int, r: int) -> None:
    """0 new, 0.5 full. Waxing lights the right, waning the left. Earthshine."""
    sun_x = math.sin(2.0 * math.pi * phase)
    sun_z = -math.cos(2.0 * math.pi * phase)
    lit = (240, 230, 200)
    shine = (58, 72, 96)
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            dx = (x - cx) / r
            dy = (y - cy) / r
            rr = dx * dx + dy * dy
            if rr > 1.0:
                continue
            nz = math.sqrt(max(0.0, 1.0 - rr))
            if dx * sun_x + nz * sun_z > 0:
                c.set(x, y, lit)
            else:
                c.set(x, y, shine)


def paint_precip(c: Canvas, weather: str, tsec: float) -> None:
    if weather == "rain" or weather == "storm":
        streak = (96, 152, 192)
        for i in range(14):
            x = (i * 5 + 3) % W
            y0 = int((tsec * 9 + i * 5) % (H + 4)) - 2
            for k in range(4):
                c.blend(x, y0 + k, streak, 0.7)
        if weather == "storm":
            # Lightning: rare, held for a beat, not a blink-star.
            flash = int(tsec) % 11 == 0
            if flash:
                bolt = ((46, 0), (48, 3), (45, 6), (47, 9), (44, 12), (46, 15))
                for (x, y) in bolt:
                    c.set(x, y, (248, 232, 112))
                    c.blend(x + 1, y, (248, 232, 112), 0.5)
    elif weather == "snow":
        flake = (228, 236, 242)
        for i in range(16):
            x = (i * 4 + int(tsec) // 2) % W
            y = int((tsec * 2.4 + i * 3) % H)
            c.set(x, y, flake)
            if i % 3 == 0:
                c.set(x + 1, y, flake)


def paint_iss(c: Canvas, iss: dict) -> None:
    east = iss["east_km"]
    # Map ±1500 km east onto the bar, clamped.
    x = int(36 + 34 * max(-1.0, min(1.0, east / ISS_NEAR_KM)))
    y = 3 if iss["state"] == "overhead" else 4
    body, accent = (192, 200, 208), (232, 80, 64)
    # Sliding 7x3 craft.
    for dx, dy, col in (
        (-3, 1, body),
        (-2, 1, body),
        (-1, 0, body),
        (-1, 1, body),
        (-1, 2, body),
        (0, 1, accent),
        (1, 0, body),
        (1, 1, body),
        (1, 2, body),
        (2, 1, body),
        (3, 1, body),
    ):
        c.set(x + dx, y + dy, col)
    if iss["state"] == "overhead":
        c.set(x, y - 1, accent)


def _best_palette_in_lum(
    hint: tuple[int, int, int], lo: float, hi: float
) -> tuple[int, int, int]:
    candidates = [rgb for rgb in _PALETTE_RGB if lo <= lum(rgb) <= hi]
    if not candidates:
        candidates = list(_PALETTE_RGB)
    r, g, b = hint
    best = candidates[0]
    best_d = 10**9
    for pr, pg, pb in candidates:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d = d
            best = (pr, pg, pb)
    return best


# ---------------------------------------------------------------------------
# Preview generation + tests
# ---------------------------------------------------------------------------


def paint_scene(
    period: str,
    weather: str,
    dt: datetime,
    iss: dict | None,
    tsec: float | None = None,
) -> Canvas:
    if tsec is None:
        tsec = dt.timestamp()
    c = Canvas()
    paint_base_sky(c, period, weather, dt, tsec)
    paint_clouds(c, weather, tsec)
    if period == "night":
        paint_stars_and_moon(c, dt, weather)
    else:
        paint_sun(c, period, weather, dt)
    paint_precip(c, weather, tsec)
    if iss and iss.get("state") in ("near", "overhead"):
        paint_iss(c, iss)
    return c


def overlay_color(
    canvas: Canvas, period: str | None = None
) -> tuple[str, tuple[int, int, int]]:
    """Always lamp. Dark 1px ink on day cyan bloomed into unreadable pale dots."""
    return LAMP, LAMP_RGB


def overlay_color_at(
    canvas: Canvas, y: int, period: str | None = None
) -> tuple[str, tuple[int, int, int]]:
    return overlay_color(canvas, period)


def _glyph_origin(text: str, cx: int, cy: int) -> tuple[int, int]:
    tw, th = text_size(text)
    return cx - tw // 2, cy - th // 2


def _glyph_pixels(text: str, cx: int, cy: int) -> list[tuple[int, int]]:
    x0, y0 = _glyph_origin(text, cx, cy)
    pts: list[tuple[int, int]] = []
    x = x0
    for ch in text:
        g = _GLYPHS.get(ch, _GLYPHS[" "])
        for gy, row in enumerate(g):
            for gx, bit in enumerate(row):
                if bit == "1":
                    pts.append((x + gx, y0 + gy))
        x += len(g[0]) + 1
    return pts


def paint_label_beds(c: Canvas, clock: str, temp: str, y_time: int, y_temp: int) -> None:
    """Deep-blue silhouette under each label so lamp overlay reads on day cyan.

    One sausage per line, 1px sky between time and temp — not a left-half plate.
    """
    for text, cy in ((clock, y_time), (temp, y_temp)):
        if cy < 0:
            continue
        pts = _glyph_pixels(text, TEXT_X, cy)
        dilated: set[tuple[int, int]] = set()
        for x, y in pts:
            for dx in range(-2, 3):
                for dy in range(-1, 2):
                    # Keep a gap between the two lines (time y~3-7, temp y~9-13).
                    if dy == -1 and cy == TEMP_Y:
                        continue
                    if dy == 1 and cy == TIME_Y:
                        continue
                    dilated.add((x + dx, y + dy))
        tw, th = text_size(text)
        x0, y0 = _glyph_origin(text, TEXT_X, cy)
        for y in range(y0, y0 + th):
            for x in range(x0 - 2, x0 + tw + 2):
                dilated.add((x, y))
        for x, y in dilated:
            c.set(x, y, BED_RGB)


def format_clock(dt: datetime) -> str:
    """12-hour numbers only. ``2:08`` not ``2:08PM`` and not ``14:08``."""
    local = dt.astimezone(TZ)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d}"


def format_temp(temp_c: int | None) -> str:
    if temp_c is None:
        return "--C"
    return f"{int(temp_c)}C"


# ---------------------------------------------------------------------------
# Draw payload
# ---------------------------------------------------------------------------

def build_elements(
    canvas: Canvas,
    clock: str,
    temp: str,
    park: bool,
    color_time: str | None = None,
    color_temp: str | None = None,
) -> list[dict]:
    y_time = PARK_Y if park else TIME_Y
    y_temp = PARK_Y if park else TEMP_Y
    if not park:
        paint_label_beds(canvas, clock, temp, y_time, y_temp)
    color_time = color_time or LAMP
    color_temp = color_temp or LAMP
    xpm = canvas.to_xpm()
    return [
        {
            "id": "frame",
            "type": "xpmbitmap",
            "display": "front",
            "align": "top_left",
            "x": 0,
            "y": 0,
            "timeout": 0,
            "data": xpm,
        },
        {
            "id": "city",
            "type": "text",
            "display": "front",
            "text": clock,
            "font": "small",
            "align": "center",
            "x": TEXT_X,
            "y": y_time,
            "color": color_time,
            "timeout": 0,
        },
        {
            "id": "temp",
            "type": "text",
            "display": "front",
            "text": temp,
            "font": "small",
            "align": "center",
            "x": TEXT_X,
            "y": y_temp,
            "color": color_temp,
            "timeout": 0,
        },
    ]


# ---------------------------------------------------------------------------
# Transport
# USB:  POST {BUSY_HOST}/api/display/draw
#       optional X-API-Token when BUSY_PIN is set (Wi-Fi PIN; USB usually skips it)
# Cloud: POST https://api.busy.app/busybar/display/draw
#       Authorization: Bearer BUSY_BAR_TOKEN, plus a desktop Chrome user agent
# Frames are replaced in place. This painter never sends DELETE /display/draw.
# ---------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.abspath(__file__))


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a .env file. Blank lines and # comments are ignored."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_dotenv(path: str | None = None) -> None:
    """Fill missing environment variables from a .env file next to this script.

    Values already set in the environment win, so a LaunchAgent or the shell
    can override the file.
    """
    path = path or os.path.join(_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            parsed = parse_dotenv(handle.read())
    except FileNotFoundError:
        return
    for key, val in parsed.items():
        os.environ.setdefault(key, val)


def _env_get(env: Mapping[str, str], name: str, default: str = "") -> str:
    val = env.get(name)
    if val is None:
        return default
    return val.strip()


@dataclass(frozen=True)
class Settings:
    transport: str
    host: str
    pin: str | None
    token: str | None
    lat: float
    lon: float
    tz_name: str
    label: str


def settings_from_map(env: Mapping[str, str]) -> Settings:
    transport = _env_get(env, "BUSY_TRANSPORT", "usb").lower()
    if transport not in ("usb", "cloud"):
        raise ValueError("BUSY_TRANSPORT must be usb or cloud")
    host = _env_get(env, "BUSY_HOST", DEFAULT_HOST).rstrip("/") or DEFAULT_HOST
    pin = _env_get(env, "BUSY_PIN") or None
    token = _env_get(env, "BUSY_BAR_TOKEN") or _env_get(env, "BUSY_BAR_CLOUD_TOKEN") or None
    lat_s = _env_get(env, "BUSY_LAT", str(DEFAULT_LAT))
    lon_s = _env_get(env, "BUSY_LON", str(DEFAULT_LON))
    try:
        lat = float(lat_s)
        lon = float(lon_s)
    except ValueError as exc:
        raise ValueError("BUSY_LAT and BUSY_LON must be numbers") from exc
    tz_name = _env_get(env, "BUSY_TZ", DEFAULT_TZ) or DEFAULT_TZ
    label = _env_get(env, "BUSY_LABEL", DEFAULT_LABEL) or DEFAULT_LABEL
    # Validate the zone now so a bad name fails before the first frame.
    ZoneInfo(tz_name)
    return Settings(transport, host, pin, token, lat, lon, tz_name, label)


def apply_place(lat: float, lon: float, tz_name: str, label: str) -> None:
    """Point astronomy and the clock at the configured place."""
    global HOME_LAT, HOME_LON, TZ, LABEL
    HOME_LAT = lat
    HOME_LON = lon
    TZ = ZoneInfo(tz_name)
    LABEL = label
    _SUN_TIMES.clear()


def local_draw_url(host: str) -> str:
    base = host.rstrip("/")
    if base.endswith("/api"):
        return base + "/display/draw"
    return base + "/api/display/draw"


def draw_request(cfg: Settings, elements: list[dict]) -> tuple[str, dict[str, str], bytes]:
    """Build the draw call. Does not send it, and never clears the screen."""
    payload = {
        "application_name": APP_NAME,
        "priority": PRIORITY,
        "elements": elements,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if cfg.transport == "cloud":
        headers["Authorization"] = f"Bearer {cfg.token or ''}"
        headers["User-Agent"] = CHROME_UA
        return CLOUD_DRAW_URL, headers, body
    headers["User-Agent"] = "busy-bar-painter"
    if cfg.pin:
        headers["X-API-Token"] = cfg.pin
    return local_draw_url(cfg.host), headers, body


def draw_elements(cfg: Settings, elements: list[dict]) -> None:
    """POST one frame. Same element ids replace the previous frame in place."""
    if cfg.transport == "cloud" and not cfg.token:
        raise RuntimeError("BUSY_BAR_TOKEN is unset")
    url, headers, body = draw_request(cfg, elements)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        hint = ""
        if exc.code == 403 and cfg.transport == "usb" and not cfg.pin:
            hint = " Set BUSY_PIN to the bar HTTP API PIN."
        raise RuntimeError(f"draw HTTP {exc.code}.{hint} {detail}".strip()) from exc


# ---------------------------------------------------------------------------
# PNG preview (stdlib)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def png_bytes(canvas: Canvas, scale: int = 12, gap: int = 1) -> bytes:
    cell = scale
    ow, oh = W * cell, H * cell
    raw = bytearray()
    bg = (12, 12, 14)
    for y in range(oh):
        raw.append(0)
        gy = y // cell
        ly = y % cell
        for x in range(ow):
            gx = x // cell
            lx = x % cell
            if lx >= cell - gap or ly >= cell - gap:
                r, g, b = bg
            else:
                r, g, b = canvas.px[gy][gx]
            raw.extend((r, g, b))
    comp = zlib.compress(bytes(raw), 9)
    ihdr = struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", comp)
        + _png_chunk(b"IEND", b"")
    )


def write_png(path: str, canvas: Canvas, scale: int = 12, gap: int = 1) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(png_bytes(canvas, scale, gap))


def composite_preview(
    canvas: Canvas,
    clock: str,
    temp: str,
    park: bool,
    period: str | None = None,
) -> Canvas:
    """Copy of the LED frame with overlay glyphs stamped for human preview."""
    out = Canvas()
    for y in range(H):
        for x in range(W):
            out.px[y][x] = canvas.px[y][x]
    if park:
        return out
    paint_label_beds(out, clock, temp, TIME_Y, TEMP_Y)
    blit_text(out, clock, TEXT_X, TIME_Y, LAMP_RGB, (12, 20, 36))
    blit_text(out, temp, TEXT_X, TEMP_Y, LAMP_RGB, (12, 20, 36))
    return out


# ---------------------------------------------------------------------------
# Health / preview HTTP
# ---------------------------------------------------------------------------

STATE: dict = {
    "ok": True,
    "app": APP_NAME,
    "label": LABEL,
    "transport": None,
    "period": None,
    "weather": None,
    "temp_c": None,
    "clock": None,
    "iss_km": None,
    "iss_state": "far",
    "last_draw_ok": None,
    "last_error": None,
    "frames": 0,
    "readable_note": (
        "Lamp overlay on tight deep-blue glyph beds. No left-half navy plate. "
        "Clock is 12-hour numbers (3:09). Period is Santa Clara solar elevation."
    ),
}
PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "previews")
_STATE_LOCK = threading.Lock()


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("health " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health", "/healthz"):
            with _STATE_LOCK:
                body = json.dumps(STATE, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class PreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("preview " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = _gallery_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if path in ("/live.png", "/previews/live.png"):
            data = png_bytes(render_live_preview())
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/previews/"):
            name = os.path.basename(path)
            fp = os.path.join(PREVIEW_DIR, name)
            if os.path.isfile(fp) and name.endswith(".png"):
                data = open(fp, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        if path in ("/health", "/healthz"):
            with _STATE_LOCK:
                body = json.dumps(STATE, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def render_live_preview() -> Canvas:
    """One live Santa Clara frame for the gallery (clouds/sun move with wall time)."""
    dt = datetime.now(TZ)
    with _STATE_LOCK:
        weather = STATE.get("weather") or "partly"
        temp_c = STATE.get("temp_c")
        iss_state = STATE.get("iss_state")
    if weather not in WEATHERS:
        weather = "partly"
    period = period_for(dt)
    iss = None
    canvas = paint_scene(period, weather, dt, iss, tsec=time.time())
    clock = format_clock(dt)
    temp = format_temp(None if temp_c is None else int(temp_c))
    return composite_preview(
        canvas, clock, temp, park=iss_state == "overhead", period=period
    )


def _gallery_html() -> str:
    cards = []
    if os.path.isdir(PREVIEW_DIR):
        for name in sorted(os.listdir(PREVIEW_DIR)):
            if name.endswith(".png"):
                label = name[:-4].replace("_", " · ")
                cards.append(
                    f'<figure><img src="/previews/{name}" alt="{label}" width="864" height="192">'
                    f"<figcaption>{label}</figcaption></figure>"
                )
    grid = "\n".join(cards) or "<p>No frames yet.</p>"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Busy Bar sky painter</title>
<style>
  body {{ background:#0c0e12; color:#efe6d6; font:14px/1.4 ui-sans-serif,system-ui; margin:0; }}
  header {{ padding:20px 24px 8px; }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p {{ max-width:70ch; color:#c8c0b4; }}
  main {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:18px; padding:16px 24px 40px; }}
  figure {{ margin:0; background:#16181e; padding:10px; border-radius:8px; }}
  header figure.live {{ max-width:720px; margin:16px 0 8px; }}
  img {{ image-rendering:pixelated; width:100%; height:auto; background:#000; }}
  figcaption {{ margin-top:8px; font-size:12px; color:#b7b0a4; }}
</style></head>
<body>
<header>
  <h1>Santa Clara sky · 72×16</h1>
  <p>Animated sky: drifting clouds, a sun that tracks solar elevation,
  weather, and night stars only after sunset. The clock is 12-hour numbers
  (<code>3:09</code>) in lamp on tight glyph beds.</p>
  <figure class="live">
    <img id="live" src="/live.png" alt="live sky" width="864" height="192">
    <figcaption>live · refreshes every second</figcaption>
  </figure>
</header>
<main>{grid}</main>
<script>
(function () {{
  var img = document.getElementById("live");
  if (!img) return;
  setInterval(function () {{ img.src = "/live.png?" + Date.now(); }}, 1000);
}})();
</script>
</body></html>"""


def start_server(handler: type[BaseHTTPRequestHandler], host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


# ---------------------------------------------------------------------------
# Painter loop
# ---------------------------------------------------------------------------

class Painter:
    def __init__(self) -> None:
        self.weather = {
            "temp_c": None,
            "weather": "clear",
            "sunrise": None,
            "sunset": None,
        }
        self.iss: dict | None = None
        self.weather_ts = 0.0
        self.iss_ts = 0.0

    def refresh(self, now: float) -> None:
        if now - self.weather_ts > 600 or not self.weather_ts:
            try:
                self.weather = fetch_weather()
                self.weather_ts = now
            except Exception as exc:  # noqa: BLE001
                log.warning("weather fetch failed: %s", exc)
                self.weather_ts = now - 540  # retry sooner
        if now - self.iss_ts > 5 or not self.iss_ts:
            try:
                got = fetch_iss()
                if got:
                    self.iss = got
                self.iss_ts = now
            except Exception as exc:  # noqa: BLE001
                log.warning("ISS fetch failed: %s", exc)
                self.iss_ts = now

    def frame(self, dt: datetime) -> tuple[Canvas, list[dict], dict]:
        period = period_for(dt)
        weather = self.weather.get("weather") or "clear"
        iss = self.iss
        park = bool(iss and iss.get("state") == "overhead")
        canvas = paint_scene(period, weather, dt, iss)
        clock = format_clock(dt)
        temp = format_temp(self.weather.get("temp_c"))
        color, _ = overlay_color(canvas, period)
        elements = build_elements(canvas, clock, temp, park, LAMP, LAMP)
        meta = {
            "period": period,
            "weather": weather,
            "clock": clock,
            "temp": temp,
            "color": color,
            "park": park,
            "iss_km": None if not iss else round(iss["dist_km"]),
            "iss_state": None if not iss else iss["state"],
        }
        return canvas, elements, meta


def run_loop(cfg: Settings, painter: Painter, once: bool = False) -> int:
    while True:
        try:
            now = time.time()
            painter.refresh(now)
            dt = datetime.now(TZ)
            canvas, elements, meta = painter.frame(dt)
            with _STATE_LOCK:
                STATE.update(
                    {
                        "ok": True,
                        "label": LABEL,
                        "transport": cfg.transport,
                        "period": meta["period"],
                        "weather": meta["weather"],
                        "temp_c": painter.weather.get("temp_c"),
                        "clock": meta["clock"],
                        "iss_km": meta["iss_km"],
                        "iss_state": meta["iss_state"] or "far",
                        "last_error": None,
                    }
                )
            if cfg.transport == "cloud" and not cfg.token:
                log.warning("BUSY_BAR_TOKEN is unset, so cloud mode is not drawing")
                with _STATE_LOCK:
                    STATE["last_draw_ok"] = None
                    STATE["frames"] = int(STATE["frames"]) + 1
            else:
                draw_elements(cfg, elements)
                with _STATE_LOCK:
                    STATE["last_draw_ok"] = True
                    STATE["frames"] = int(STATE["frames"]) + 1
            log.info(
                "frame %s %s %s %s iss=%s km=%s park=%s",
                meta["clock"],
                meta["temp"],
                meta["period"],
                meta["weather"],
                meta["iss_state"],
                meta["iss_km"],
                meta["park"],
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            log.error("cannot reach the bar: %s", reason)
            with _STATE_LOCK:
                STATE["ok"] = True
                STATE["last_error"] = str(reason)
                STATE["last_draw_ok"] = False
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the painter
            log.error("draw-loop exception: %s\n%s", exc, traceback.format_exc())
            with _STATE_LOCK:
                STATE["ok"] = True  # process still alive
                STATE["last_error"] = str(exc)
                STATE["last_draw_ok"] = False
        if once:
            with _STATE_LOCK:
                return 0 if STATE.get("last_draw_ok") else 1
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Preview generation + tests
# ---------------------------------------------------------------------------

_PREVIEW_TIMES = {
    "morning": datetime(2026, 9, 8, 6, 48, tzinfo=TZ),
    "day": datetime(2026, 9, 8, 13, 10, tzinfo=TZ),
    "evening": datetime(2026, 9, 8, 19, 18, tzinfo=TZ),
    "night": datetime(2026, 9, 8, 22, 40, tzinfo=TZ),
}
_PREVIEW_TEMP = {
    "morning": 16,
    "day": 24,
    "evening": 20,
    "night": 14,
}


def representative_dt(period: str) -> datetime:
    return _PREVIEW_TIMES[period]


def write_preview_set(out_dir: str = PREVIEW_DIR) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    weathers = ("clear", "cloudy", "fog", "rain")
    for period in PERIODS:
        dt = representative_dt(period)
        for weather in weathers:
            canvas = paint_scene(period, weather, dt, None)
            clock = format_clock(dt)
            temp = format_temp(_PREVIEW_TEMP[period])
            vis = composite_preview(canvas, clock, temp, park=False, period=period)
            name = f"{period}_{weather}.png"
            path = os.path.join(out_dir, name)
            write_png(path, vis)
            written.append(path)
    # Extra scenes the user called out.
    extras = (
        ("night", "clear", "night_stars.png"),
        ("night", "fog", "night_fog.png"),
        ("morning", "partly", "morning_partly.png"),
        ("day", "storm", "day_storm.png"),
        ("day", "snow", "day_snow.png"),
    )
    for period, weather, name in extras:
        dt = representative_dt(period)
        canvas = paint_scene(period, weather, dt, None)
        vis = composite_preview(
            canvas, format_clock(dt), format_temp(_PREVIEW_TEMP[period]), False, period
        )
        path = os.path.join(out_dir, name)
        write_png(path, vis)
        written.append(path)
    # Overhead ISS parks the labels.
    dt = representative_dt("night")
    iss = {
        "east_km": 40.0,
        "north_km": 10.0,
        "dist_km": 430.0,
        "state": "overhead",
        "lat": HOME_LAT,
        "lon": HOME_LON,
        "alt_km": 420.0,
    }
    canvas = paint_scene("night", "clear", dt, iss)
    vis = composite_preview(canvas, format_clock(dt), "14C", park=True, period="night")
    path = os.path.join(out_dir, "night_iss_overhead.png")
    write_png(path, vis)
    written.append(path)
    # What the live 09:05 PT photo should have been: day, not a morning wash.
    photo = datetime(2026, 9, 8, 9, 5, tzinfo=TZ)
    canvas = paint_scene("day", "clear", photo, None)
    vis = composite_preview(canvas, format_clock(photo), "18C", False, "day")
    path = os.path.join(out_dir, "photo_time_0905_day.png")
    write_png(path, vis)
    written.append(path)
    # Afternoon hardware photo: full-width day sky, no dark left plate.
    photo_240 = datetime(2026, 9, 8, 14, 40, tzinfo=TZ)
    canvas = paint_scene(period_for(photo_240), "clear", photo_240, None)
    vis = composite_preview(canvas, format_clock(photo_240), "31C", False, "day")
    path = os.path.join(out_dir, "photo_time_1440_day.png")
    write_png(path, vis)
    written.append(path)
    # 2:08 PM PT: day sky + 12-hour numbers, not night and not 14:08.
    photo_208 = datetime(2026, 9, 8, 14, 8, tzinfo=TZ)
    canvas = paint_scene(period_for(photo_208), "partly", photo_208, None, tsec=1000.0)
    vis = composite_preview(canvas, format_clock(photo_208), "31C", False, "day")
    path = os.path.join(out_dir, "photo_time_1408_day.png")
    write_png(path, vis)
    written.append(path)
    canvas8 = paint_scene(period_for(photo_208), "partly", photo_208, None, tsec=1008.0)
    vis8 = composite_preview(canvas8, format_clock(photo_208), "31C", False, "day")
    path = os.path.join(out_dir, "photo_time_1408_day_drift.png")
    write_png(path, vis8)
    written.append(path)
    # 3:09 PM hardware photo: lamp on tight beds, full-width day sky.
    photo_309 = datetime(2026, 9, 8, 15, 9, tzinfo=TZ)
    canvas = paint_scene(period_for(photo_309), "clear", photo_309, None)
    vis = composite_preview(canvas, format_clock(photo_309), "32C", False, "day")
    path = os.path.join(out_dir, "photo_time_1509_day.png")
    write_png(path, vis)
    written.append(path)
    return written


def _glyph_contrast(canvas: Canvas, text: str, cx: int, cy: int, rgb: tuple[int, int, int]) -> float:
    tw, th = text_size(text)
    x0 = cx - tw // 2
    y0 = cy - th // 2
    diffs: list[float] = []
    x = x0
    for ch in text:
        g = _GLYPHS.get(ch, _GLYPHS[" "])
        for gy, row in enumerate(g):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                sky = canvas.get(x + gx, y0 + gy)
                diffs.append(abs(lum(sky) - lum(rgb)))
        x += len(g[0]) + 1
    return min(diffs) if diffs else 0.0


def _dark_left_block(canvas: Canvas) -> bool:
    """True if a wide HUD plate covers the left, not tight glyph beds.

    Glyph sausages are ~22px wide. The old plate filled x=5..35 on most rows.
    """
    wide_rows = 0
    for y in range(1, 15):
        dark_cols = sum(1 for x in range(5, 36) if lum(canvas.get(x, y)) <= 0.18)
        if dark_cols >= 26:
            wide_rows += 1
    return wide_rows >= 10


def _tight_beds_present(canvas: Canvas, clock: str, temp: str) -> bool:
    """Lamp overlay needs a dark silhouette under the numbers."""
    n = 0
    for text, cy in ((clock, TIME_Y), (temp, TEMP_Y)):
        for x, y in _glyph_pixels(text, TEXT_X, cy):
            if lum(canvas.get(x, y)) <= 0.28:
                n += 1
    return n >= 12


def self_test() -> int:
    apply_place(DEFAULT_LAT, DEFAULT_LON, DEFAULT_TZ, DEFAULT_LABEL)
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # Palette completeness: every written glyph exists.
    for period in PERIODS:
        for weather in WEATHERS:
            dt = representative_dt(period)
            canvas = paint_scene(period, weather, dt, None)
            try:
                xpm = canvas.to_xpm()
            except KeyError as exc:
                failures.append(f"palette KeyError {exc} on {period}/{weather}")
                continue
            lines = xpm.splitlines()
            check(lines[0] == "! XPM2", "XPM2 signature")
            w, h, ncolors, cpp = map(int, lines[1].split()[:4])
            check(w == 72 and h == 16, "size")
            check(ncolors <= 32, f"ncolors {ncolors}")
            check(cpp == 1, "cpp")
            color_lines = lines[2 : 2 + ncolors]
            rows = lines[2 + ncolors :]
            table = {}
            for cl in color_lines:
                ch, vis, val = cl[0], cl[2], cl[4:]
                check(vis == "c", f"visual {vis}")
                check(val.startswith("#") and len(val) == 7, f"color {val}")
                check("A" not in val[1:] or len(val) == 7, "no alpha")
                table[ch] = val
            check("S" not in "".join(rows) or "S" in table, "S mapped")
            for row in rows:
                check(len(row) == 72, "row width")
                for ch in row:
                    if ch not in table:
                        failures.append(f"missing palette key {ch!r} on {period}/{weather}")
                        break

            clock, temp = format_clock(dt), format_temp(20)
            labeled = composite_preview(canvas, clock, temp, False, period)
            try:
                labeled.to_xpm()
            except KeyError as exc:
                failures.append(f"labeled palette KeyError {exc} on {period}/{weather}")
            color, _ = overlay_color(canvas, period)
            payload_canvas = paint_scene(period, weather, dt, None)
            els = build_elements(payload_canvas, clock, temp, False, color, color)
            city = next(e for e in els if e["id"] == "city")
            check(city["color"] == LAMP, f"overlay not lamp on {period}/{weather}")
            check(city["color"] != "#FFFFFFFF", "never default white")
            check("z_index" not in city and "width" not in city, "no z_index/width")
            sky = next(e for e in els if e["type"] == "xpmbitmap")
            check(sky["id"] == "frame" and sky.get("align") == "top_left", "frame top_left")
            check("z_index" not in sky, "xpm has no z_index")
            check("! XPM2" in sky["data"], "xpm in payload")
            check("AM" not in clock and "PM" not in clock, f"clock has no AM/PM, got {clock}")
            check(":" in clock, f"clock missing colon: {clock}")
            if period in ("morning", "day") and weather in ("clear", "partly"):
                if _dark_left_block(payload_canvas):
                    failures.append(f"daytime dark left plate on {period}/{weather}")
                if not _tight_beds_present(payload_canvas, clock, temp):
                    failures.append(f"missing glyph beds on {period}/{weather}")

            bed = canvas.sample_avg(40, 2, 70, 13)
            if period == "morning" and weather == "clear":
                check(lum(bed) < 0.85, f"morning wash too pale L={lum(bed):.2f}")
                r, g, b = bed
                check(not (r > 210 and g > 210 and b > 210), "morning bed is near-white")

            if weather == "fog":
                r, g, b = canvas.sample_avg(40, 2, 70, 13)
                mx = max(r, g, b) + 1
                sat = (mx - min(r, g, b)) / mx
                check(sat < 0.35, f"fog should be desaturated, sat={sat:.2f}")

    # Moon phases.
    c = Canvas((8, 12, 20))
    paint_moon(c, 0.0, 8, 8, 3)
    paint_moon(c, 0.5, 24, 8, 3)
    paint_moon(c, 0.25, 40, 8, 3)
    paint_moon(c, 0.75, 56, 8, 3)
    new_lit = sum(1 for y in range(H) for x in range(5, 12) if c.get(x, y) == nearest_palette((240, 230, 200)))
    full_lit = sum(1 for y in range(H) for x in range(21, 28) if c.get(x, y) == nearest_palette((240, 230, 200)))
    check(full_lit > new_lit + 5, "full moon brighter than new")
    # Waxing (0.25): light on the right of center 40.
    wax_r = sum(1 for y in range(5, 12) for x in range(41, 44) if c.get(x, y) == nearest_palette((240, 230, 200)))
    wax_l = sum(1 for y in range(5, 12) for x in range(37, 40) if c.get(x, y) == nearest_palette((240, 230, 200)))
    check(wax_r >= wax_l, "waxing lights the right")
    wan_r = sum(1 for y in range(5, 12) for x in range(57, 60) if c.get(x, y) == nearest_palette((240, 230, 200)))
    wan_l = sum(1 for y in range(5, 12) for x in range(53, 56) if c.get(x, y) == nearest_palette((240, 230, 200)))
    check(wan_l >= wan_r, "waning lights the left")

    # ISS parks labels.
    iss = {"east_km": 0, "north_km": 0, "dist_km": 400, "state": "overhead", "lat": HOME_LAT, "lon": HOME_LON, "alt_km": 420}
    canvas = paint_scene("night", "clear", representative_dt("night"), iss)
    els = build_elements(canvas, "22:40", "14C", park=True)
    city = next(e for e in els if e["id"] == "city")
    temp_el = next(e for e in els if e["id"] == "temp")
    check(city["y"] == PARK_Y and temp_el["y"] == PARK_Y, "ISS overhead parks text")
    check(city["timeout"] == 0 and city["font"] == "small", "overlay fields")
    check(els[0]["type"] == "xpmbitmap" and els[0]["id"] == "frame", "frame xpmbitmap")
    check("DELETE" not in json.dumps(els), "no delete")

    # Real Santa Clara period at the photo timestamp should not be a
    # hard-coded clock bucket. 09:05 PT on 2026-09-08 is well after sunrise.
    photo_t = datetime(2026, 9, 8, 9, 5, tzinfo=TZ)
    p = period_for(photo_t)
    check(p in ("day", "morning"), f"photo time period {p}")
    _, el = solar_position(photo_t)
    check(el > 20, f"09:05 should be a high sun, el={el:.1f}")
    if el > 25:
        check(p == "day", f"09:05 with el={el:.1f} must be day, not a fixed morning clock")

    # Day: lamp on tight beds, sky still open on the left — not ink-on-cyan.
    photo_309 = datetime(2026, 9, 8, 15, 9, tzinfo=TZ)
    canvas = paint_scene("day", "clear", photo_309, None)
    check(not _dark_left_block(canvas), "sky itself has no plate")
    color, rgb = overlay_color(canvas, "day")
    check(color == LAMP, f"day overlay should be lamp, got {color}")
    els = build_elements(canvas, format_clock(photo_309), "32C", False, LAMP, LAMP)
    check(format_clock(photo_309) == "3:09", f"clock {format_clock(photo_309)}")
    check(not _dark_left_block(canvas), "3:09 XPM must not be a dark left plate")
    check(_tight_beds_present(canvas, "3:09", "32C"), "3:09 has glyph beds")
    margin = lum(canvas.sample_avg(1, 1, 6, 14))
    right = lum(canvas.sample_avg(48, 1, 70, 14))
    check(margin > 0.22, f"left margin still sky L={margin:.2f}")
    check(margin >= right * 0.45, f"left margin hole {margin:.2f}/{right:.2f}")
    bed = canvas.sample_avg(TEXT_X - 4, TIME_Y - 2, TEXT_X + 4, TIME_Y + 2)
    check(lum(bed) < 0.30, f"glyph bed should be dark under 3:09 L={lum(bed):.2f}")
    check(abs(lum(bed) - lum(LAMP_RGB)) > 0.45, "lamp vs bed contrast")
    city = next(e for e in els if e["id"] == "city")
    check(city["text"] == "3:09" and city["color"] == LAMP, "3:09 lamp overlay")
    night_c = paint_scene("night", "clear", representative_dt("night"), None)
    ncolor, _ = overlay_color(night_c, "night")
    check(ncolor == LAMP, f"night overlay should be lamp, got {ncolor}")

    # 2:08 PM PT: must be day + 12-hour numbers, never night stars, never 14:08.
    photo_208 = datetime(2026, 9, 8, 14, 8, tzinfo=TZ)
    check(period_for(photo_208) == "day", f"14:08 period={period_for(photo_208)}")
    check(format_clock(photo_208) == "2:08", f"clock {format_clock(photo_208)}")
    check(format_clock(photo_208) != "14:08", "not 24-hour")
    _, el_208 = solar_position(photo_208)
    check(el_208 > 40, f"2:08 sun should be high, el={el_208:.1f}")
    photo_131 = datetime(2026, 9, 8, 13, 31, tzinfo=TZ)
    check(period_for(photo_131) == "day", f"13:31 period={period_for(photo_131)}")
    check(format_clock(photo_131) == "1:31", f"clock {format_clock(photo_131)}")
    # The UTC-misparsed Open-Meteo sunset that used to force night.
    bogus_set = datetime.fromisoformat("2026-09-08T19:25:00").astimezone(TZ)
    bogus_rise = datetime.fromisoformat("2026-09-08T06:44:00").astimezone(TZ)
    check(period_for(photo_208, bogus_rise, bogus_set) == "day", "ignore UTC-shifted API sunset")
    check(period_for(datetime(2026, 9, 8, 10, 27, tzinfo=TZ), bogus_rise, bogus_set) == "day", "10:27 is day not evening")
    check(period_for(datetime(2026, 9, 8, 22, 40, tzinfo=TZ)) == "night", "22:40 is night")
    check(format_clock(datetime(2026, 9, 8, 22, 40, tzinfo=TZ)) == "10:40", "night clock")
    check(format_clock(datetime(2026, 9, 8, 0, 5, tzinfo=TZ)) == "12:05", "midnight clock")

    rise_sc, set_sc = sunrise_sunset(photo_208)
    check(period_for(set_sc - timedelta(minutes=1)) == "evening", "minute before sunset is evening")
    check(period_for(set_sc + timedelta(minutes=1)) == "night", "night only after sunset")
    check(period_for(rise_sc - timedelta(minutes=1)) == "night", "still night just before sunrise")
    check(period_for(rise_sc + timedelta(minutes=1)) == "morning", "morning just after sunrise")

    day = paint_scene("day", "clear", photo_208, None)
    sky_r = day.sample_avg(48, 1, 70, 7)
    check(lum(sky_r) > 0.28, f"2:08 day sky should be bright, not night L={lum(sky_r):.2f}")
    check(not _dark_left_block(day), "2:08 day XPM has no dark left plate")
    els = build_elements(day, format_clock(photo_208), "31C", False, LAMP, LAMP)
    check(not _dark_left_block(day), "payload XPM still has no left plate")
    check(_tight_beds_present(day, "2:08", "31C"), "2:08 payload has glyph beds")
    city = next(e for e in els if e["id"] == "city")
    temp_el = next(e for e in els if e["id"] == "temp")
    check(city["text"] == "2:08" and temp_el["text"] == "31C", "payload clock/temp")
    check(city["font"] == "small" and temp_el["font"] == "small", "native small font")
    check(next(e["id"] for e in els if e["type"] == "xpmbitmap") == "frame", "id frame")
    check(all("z_index" not in e and "width" not in e for e in els), "payload rejects z_index/width")
    check("align" in els[0] and els[0]["align"] == "top_left", "align top_left")

    a = paint_scene("day", "partly", photo_208, None, tsec=1000.0)
    b = paint_scene("day", "partly", photo_208, None, tsec=1008.0)
    drifted = sum(1 for y in range(H) for x in range(W) if a.get(x, y) != b.get(x, y))
    check(drifted > 20, f"clouds should drift between frames, changed={drifted}")
    a_clear = paint_scene("day", "clear", photo_208, None, tsec=100.0)
    b_clear = paint_scene("day", "clear", photo_208, None, tsec=108.0)
    drifted_c = sum(
        1 for y in range(H) for x in range(W) if a_clear.get(x, y) != b_clear.get(x, y)
    )
    check(drifted_c > 8, f"clear-day cirrus should still move, changed={drifted_c}")
    sx_am, sy_am, _ = _sun_xy("morning", datetime(2026, 9, 8, 6, 50, tzinfo=TZ))
    sx_pm, sy_pm, _ = _sun_xy("day", photo_208)
    check(sy_pm < sy_am, f"2:08 sun should be higher than dawn ({sy_pm} vs {sy_am})")
    # Cloud motion must not toggle a dark plate on/off (the "goes dark" flicker).
    for t in (100.0, 104.0, 108.0, 112.0):
        fr = paint_scene("day", "clear", photo_208, None, tsec=t)
        check(not _dark_left_block(fr), f"day frame at t={t} grew a dark plate")

    # Transport: USB is the default, cloud keeps the Bearer + Chrome path.
    usb = settings_from_map({})
    check(usb.transport == "usb", f"default transport {usb.transport}")
    check(usb.host == DEFAULT_HOST, f"default host {usb.host}")
    check(usb.pin is None and usb.token is None, "no secrets by default")
    check(usb.lat == DEFAULT_LAT and usb.lon == DEFAULT_LON, "default place")
    check(usb.tz_name == DEFAULT_TZ and usb.label == DEFAULT_LABEL, "default label")
    url, headers, body = draw_request(usb, build_elements(Canvas(), "2:08", "20C", False))
    check(url == "http://10.0.4.20/api/display/draw", f"usb url {url}")
    check("X-API-Token" not in headers and "Authorization" not in headers, "usb has no auth header")
    check(b"DELETE" not in body and b'"id": "frame"' in body, "usb body overwrites frame")
    pinned = settings_from_map({"BUSY_PIN": "1234", "BUSY_HOST": "http://10.0.4.20/"})
    url, headers, _ = draw_request(pinned, [])
    check(url == "http://10.0.4.20/api/display/draw", f"slash host {url}")
    check(headers.get("X-API-Token") == "1234", "PIN is X-API-Token")
    check("Authorization" not in headers, "PIN is not a Bearer token")
    cloud = settings_from_map({"BUSY_TRANSPORT": "cloud", "BUSY_BAR_TOKEN": "tok"})
    url, headers, body = draw_request(cloud, [])
    check(url == CLOUD_DRAW_URL, f"cloud url {url}")
    check(headers.get("Authorization") == "Bearer tok", "cloud bearer")
    check("Chrome/" in headers.get("User-Agent", ""), "cloud chrome user agent")
    check("X-API-Token" not in headers, "cloud does not send the USB PIN header")
    check(b"DELETE" not in body, "cloud body has no delete")
    alias = settings_from_map({"BUSY_TRANSPORT": "cloud", "BUSY_BAR_CLOUD_TOKEN": "old"})
    _, headers, _ = draw_request(alias, [])
    check(headers.get("Authorization") == "Bearer old", "old cloud token name still works")
    prefer = settings_from_map(
        {"BUSY_TRANSPORT": "cloud", "BUSY_BAR_TOKEN": "new", "BUSY_BAR_CLOUD_TOKEN": "old"}
    )
    _, headers, _ = draw_request(prefer, [])
    check(headers.get("Authorization") == "Bearer new", "BUSY_BAR_TOKEN wins")
    dotted = parse_dotenv('# comment\nexport BUSY_PIN="12 34"\nBUSY_LAT=1\n\n')
    check(dotted.get("BUSY_PIN") == "12 34" and dotted.get("BUSY_LAT") == "1", "dotenv parse")
    check("comment" not in dotted, "dotenv skips comments")
    try:
        settings_from_map({"BUSY_TRANSPORT": "wifi"})
        failures.append("wifi is not a transport")
    except ValueError:
        pass
    for hour in range(24):
        sample = datetime(2026, 9, 8, hour, 7, tzinfo=TZ)
        text = format_clock(sample)
        check("AM" not in text and "PM" not in text and ":" in text, f"clock {text}")
        check(text.count(":") == 1 and len(text.split(":")[1]) == 2, f"hour:minute {text}")

    if failures:
        print("SELF-TEST FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("SELF-TEST OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Busy Bar 72x16 sky painter")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--preview", action="store_true", help="write preview PNGs and serve the gallery")
    parser.add_argument("--once", action="store_true", help="draw one frame and exit")
    parser.add_argument("--no-serve", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.self_test:
        return self_test()

    load_dotenv()
    try:
        cfg = settings_from_map(os.environ)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        log.error("configuration: %s", exc)
        return 2
    apply_place(cfg.lat, cfg.lon, cfg.tz_name, cfg.label)
    log.info(
        "place %s lat=%s lon=%s tz=%s transport=%s",
        cfg.label,
        cfg.lat,
        cfg.lon,
        cfg.tz_name,
        cfg.transport,
    )
    if cfg.transport == "usb":
        log.info("usb draw %s pin=%s", local_draw_url(cfg.host), "set" if cfg.pin else "unset")
    else:
        log.info("cloud draw %s token=%s", CLOUD_DRAW_URL, "set" if cfg.token else "unset")

    if args.preview:
        write_preview_set()
        log.info("wrote previews to %s", PREVIEW_DIR)
        if not args.no_serve:
            start_server(HealthHandler, HEALTH_HOST, HEALTH_PORT)
            start_server(PreviewHandler, PREVIEW_HOST, PREVIEW_PORT)
            log.info("health on http://%s:%s/health", HEALTH_HOST, HEALTH_PORT)
            log.info("preview on http://%s:%s/", PREVIEW_HOST, PREVIEW_PORT)
        else:
            return 0

    painter = Painter()
    return run_loop(cfg, painter, once=args.once)


if __name__ == "__main__":
    sys.exit(main())

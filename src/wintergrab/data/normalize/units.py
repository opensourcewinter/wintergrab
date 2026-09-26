"""Measurements: ``"1.5 kg"``, ``"5'11\\""``, ``"64GB"``, ``"1,200 sq ft"`` -> :class:`Quantity`, and conversions.

Every unit belongs to a dimension (mass, length, volume, area, data, temperature,
power, energy, voltage, current, charge, frequency, time, speed) with a canonical
unit (kg, m, l, m2, B, degC, W, J, V, A, Ah, Hz, s, m/s). Conversions use exact
``Decimal`` factors (US customary for gallons, pints and fluid ounces).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ...errors import ConfigurationError
from .numbers import _note, as_int_or_float, iter_numbers

__all__ = ["UNITS", "Quantity", "convert", "parse_dimensions", "parse_quantity", "unit_info"]

D = Decimal

# unit -> (dimension, factor to the canonical unit); temperatures are handled separately (affine).
UNITS: dict[str, tuple[str, Decimal]] = {
    # mass (kg)
    "kg": ("mass", D(1)), "g": ("mass", D("0.001")), "mg": ("mass", D("0.000001")), "t": ("mass", D(1000)),
    "lb": ("mass", D("0.45359237")), "oz": ("mass", D("0.028349523125")), "st": ("mass", D("6.35029318")),
    # length (m)
    "m": ("length", D(1)), "cm": ("length", D("0.01")), "mm": ("length", D("0.001")), "km": ("length", D(1000)),
    "um": ("length", D("0.000001")), "in": ("length", D("0.0254")), "ft": ("length", D("0.3048")),
    "yd": ("length", D("0.9144")), "mi": ("length", D("1609.344")),
    # volume (l)
    "l": ("volume", D(1)), "ml": ("volume", D("0.001")), "cl": ("volume", D("0.01")), "dl": ("volume", D("0.1")),
    "m3": ("volume", D(1000)), "cm3": ("volume", D("0.001")), "gal": ("volume", D("3.785411784")),
    "qt": ("volume", D("0.946352946")), "pt": ("volume", D("0.473176473")), "floz": ("volume", D("0.0295735295625")),
    # area (m2)
    "m2": ("area", D(1)), "cm2": ("area", D("0.0001")), "km2": ("area", D(1_000_000)), "ft2": ("area", D("0.09290304")),
    "in2": ("area", D("0.00064516")), "acre": ("area", D("4046.8564224")), "ha": ("area", D(10000)),
    # data (bytes)
    "B": ("data", D(1)), "kB": ("data", D(1000)), "MB": ("data", D(10**6)), "GB": ("data", D(10**9)),
    "TB": ("data", D(10**12)), "PB": ("data", D(10**15)), "KiB": ("data", D(1024)), "MiB": ("data", D(1024**2)),
    "GiB": ("data", D(1024**3)), "TiB": ("data", D(1024**4)),
    # power, energy, electricity, frequency
    "W": ("power", D(1)), "kW": ("power", D(1000)), "MW": ("power", D(10**6)), "hp": ("power", D("745.69987158227022")),
    "J": ("energy", D(1)), "kJ": ("energy", D(1000)), "Wh": ("energy", D(3600)), "kWh": ("energy", D(3_600_000)),
    "cal": ("energy", D("4.184")), "kcal": ("energy", D(4184)),
    "V": ("voltage", D(1)), "mV": ("voltage", D("0.001")), "kV": ("voltage", D(1000)),
    "A": ("current", D(1)), "mA": ("current", D("0.001")),
    "Hz": ("frequency", D(1)), "kHz": ("frequency", D(1000)), "MHz": ("frequency", D(10**6)),
    "GHz": ("frequency", D(10**9)),
    # time (s) and speed (m/s)
    "s": ("time", D(1)), "ms": ("time", D("0.001")), "min": ("time", D(60)), "h": ("time", D(3600)),
    "day": ("time", D(86400)),
    "m/s": ("speed", D(1)), "km/h": ("speed", D(1) / D("3.6")), "mph": ("speed", D("0.44704")),
    "kn": ("speed", D("0.514444444444444444")),
}  # fmt: skip
_TEMPERATURE = {"degC", "degF", "K"}
_CANONICAL = {
    "mass": "kg", "length": "m", "volume": "l", "area": "m2", "data": "B", "power": "W", "energy": "J",
    "voltage": "V", "current": "A", "frequency": "Hz", "time": "s", "speed": "m/s", "temperature": "degC",
}  # fmt: skip

# How units are written -> canonical symbol. Matched case-insensitively unless listed in _CASED.
_SPELLINGS: dict[str, str] = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "kilogramme": "kg",
    "g": "g", "gr": "g", "gram": "g", "grams": "g", "gramme": "g", "grammes": "g", "mg": "mg", "milligram": "mg",
    "milligrams": "mg", "t": "t", "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb", "oz": "oz", "ounce": "oz", "ounces": "oz",
    "st": "st", "stone": "st",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m", "cm": "cm", "centimeter": "cm",
    "centimeters": "cm", "centimetre": "cm", "centimetres": "cm", "mm": "mm", "millimeter": "mm", "millimeters": "mm",
    "millimetre": "mm", "millimetres": "mm", "km": "km", "kilometer": "km", "kilometers": "km", "kilometre": "km",
    "kilometres": "km", "µm": "um", "um": "um", "micron": "um", "microns": "um", "in": "in", "inch": "in",
    "inches": "in", '"': "in", "”": "in", "ft": "ft", "foot": "ft", "feet": "ft", "'": "ft", "’": "ft",
    "yd": "yd", "yard": "yd", "yards": "yd", "mi": "mi", "mile": "mi", "miles": "mi",
    "l": "l", "lt": "l", "ltr": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l", "ml": "ml",
    "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml", "cl": "cl", "dl": "dl",
    "m³": "m3", "m3": "m3", "cm³": "cm3", "cm3": "cm3", "cc": "cm3", "gal": "gal", "gallon": "gal", "gallons": "gal",
    "qt": "qt", "quart": "qt", "quarts": "qt", "pt": "pt", "pint": "pt", "pints": "pt", "fl oz": "floz",
    "fl. oz": "floz", "fl.oz": "floz", "floz": "floz", "fluid ounce": "floz", "fluid ounces": "floz",
    "m²": "m2", "m2": "m2", "sqm": "m2", "sq m": "m2", "sq. m": "m2", "square meter": "m2", "square meters": "m2",
    "square metre": "m2", "square metres": "m2", "cm²": "cm2", "cm2": "cm2", "km²": "km2", "km2": "km2",
    "ft²": "ft2", "ft2": "ft2", "sq ft": "ft2", "sq. ft": "ft2", "sq.ft": "ft2", "sqft": "ft2", "square feet": "ft2",
    "square foot": "ft2", "in²": "in2", "sq in": "in2", "acre": "acre", "acres": "acre", "ha": "ha",
    "hectare": "ha", "hectares": "ha",
    "b": "B", "byte": "B", "bytes": "B", "kb": "kB", "kilobyte": "kB", "kilobytes": "kB", "mb": "MB",
    "megabyte": "MB", "megabytes": "MB", "gb": "GB", "gigabyte": "GB", "gigabytes": "GB", "tb": "TB",
    "terabyte": "TB", "terabytes": "TB", "pb": "PB", "kib": "KiB", "mib": "MiB", "gib": "GiB", "tib": "TiB",
    "w": "W", "watt": "W", "watts": "W", "kw": "kW", "kilowatt": "kW", "kilowatts": "kW", "mw": "MW",
    "hp": "hp", "horsepower": "hp", "j": "J", "joule": "J", "joules": "J", "kj": "kJ", "wh": "Wh", "kwh": "kWh",
    "cal": "cal", "kcal": "kcal", "calories": "kcal",
    "v": "V", "volt": "V", "volts": "V", "mv": "mV", "kv": "kV", "a": "A", "amp": "A", "amps": "A", "ampere": "A",
    "amperes": "A", "ma": "mA", "mah": "mAh", "hz": "Hz", "hertz": "Hz", "khz": "kHz", "mhz": "MHz", "ghz": "GHz",
    "sec": "s", "secs": "s", "second": "s", "seconds": "s", "ms": "ms", "min": "min", "mins": "min", "minute": "min",
    "minutes": "min", "hr": "h", "hrs": "h", "hour": "h", "hours": "h", "day": "day", "days": "day",
    "km/h": "km/h", "kmh": "km/h", "kph": "km/h", "mph": "mph", "m/s": "m/s", "kn": "kn", "knot": "kn", "knots": "kn",
    "°c": "degC", "ºc": "degC", "℃": "degC", "celsius": "degC", "deg c": "degC", "°f": "degF", "ºf": "degF",
    "℉": "degF", "fahrenheit": "degF", "deg f": "degF", "kelvin": "K",
}  # fmt: skip
# Spellings whose case matters: "Mb" is a megabit, "mW" a milliwatt, "K" kelvin...
_CASED: dict[str, str] = {"Mb": "Mbit", "Gb": "Gbit", "Kb": "kbit", "kb": "kB", "mW": "mW", "K": "K", "MW": "MW"}
UNITS.update({"Mbit": ("data", D(125_000)), "Gbit": ("data", D(125_000_000)), "kbit": ("data", D(125)),
              "mW": ("power", D("0.001")), "Ah": ("charge", D(1)), "mAh": ("charge", D("0.001")),
              "mA": ("current", D("0.001"))})  # fmt: skip
_CANONICAL["charge"] = "Ah"
_SPELLINGS["ah"] = "Ah"

_SPELLING_RE = re.compile(
    r"\s*("
    + "|".join(re.escape(s) for s in sorted(set(_SPELLINGS) | set(_CASED), key=len, reverse=True))
    + r")(?![A-Za-z²³])",
    re.I,
)
# Unit spellings that are also ordinary words: only units when no word follows ("5 in", not "5 in stock").
_WORDLIKE = frozenset({"in", "a", "st", "pt", "t", "min", "day", "days"})
_NEXT_WORD = re.compile(r"\s+[A-Za-z]{2,}")
_FEET_INCHES = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:'|’|ft|feet|foot)\s*(\d+(?:\.\d+)?)\s*(?:\"|”|''|in|inch|inches)?(?![A-Za-z])", re.I
)


@dataclass(frozen=True)
class Quantity:
    """A measured value. ``unit`` is a key of :data:`UNITS` (or ``degC``/``degF``/``K``)."""

    value: Decimal
    unit: str

    @property
    def dimension(self) -> str:
        return "temperature" if self.unit in _TEMPERATURE else UNITS[self.unit][0]

    def to(self, unit: str) -> Quantity:
        return convert(self, unit)

    def canonical(self) -> Quantity:
        """In the dimension's canonical unit (kg, m, l, m2, B, degC...)."""
        return convert(self, _CANONICAL[self.dimension])

    def to_dict(self) -> dict[str, Any]:
        return {"value": as_int_or_float(self.value), "unit": self.unit}

    def __str__(self) -> str:
        return f"{self.value} {self.unit}"


def unit_info(unit: str) -> tuple[str, str]:
    """``(canonical symbol, dimension)`` for a unit spelling (``"lbs"`` -> ``("lb", "mass")``)."""
    symbol = _symbol(unit)
    if symbol is None:
        raise ConfigurationError(f"unknown unit {unit!r}", key="unit")
    return symbol, ("temperature" if symbol in _TEMPERATURE else UNITS[symbol][0])


def _symbol(spelling: str) -> str | None:
    spelling = spelling.strip()
    if spelling in ("C", "F"):  # as a unit argument ({"unit": "C"}) these are temperatures
        return "deg" + spelling
    if spelling in _CASED:
        return _CASED[spelling]
    if spelling in UNITS or spelling in _TEMPERATURE:
        return spelling
    return _SPELLINGS.get(spelling.lower())


def _to_celsius(value: Decimal, unit: str) -> Decimal:
    if unit == "degF":
        return (value - 32) * 5 / 9
    if unit == "K":
        return value - D("273.15")
    return value


def _from_celsius(value: Decimal, unit: str) -> Decimal:
    if unit == "degF":
        return value * 9 / 5 + 32
    if unit == "K":
        return value + D("273.15")
    return value


def convert(quantity: Quantity, unit: str) -> Quantity:
    """``quantity`` expressed in ``unit`` (any spelling). Raises ``ValueError`` across dimensions."""
    target = _symbol(unit)
    if target is None:
        raise ConfigurationError(f"unknown unit {unit!r}", key="unit")
    source_dim = quantity.dimension
    target_dim = "temperature" if target in _TEMPERATURE else UNITS[target][0]
    if source_dim != target_dim:
        raise ValueError(f"cannot convert {source_dim} ({quantity.unit}) to {target_dim} ({target})")
    if source_dim == "temperature":
        return Quantity(_from_celsius(_to_celsius(quantity.value, quantity.unit), target), target)
    base = quantity.value * UNITS[quantity.unit][1]
    return Quantity(base / UNITS[target][1], target)


def parse_quantity(
    text: str | None, *, default_unit: str | None = None, notes: list[str] | None = None
) -> Quantity | None:
    """The first number with a unit in ``text``: ``"Weight: 1.5 kg"`` -> ``Quantity(1.5, "kg")``.

    ``5'11"`` and ``5 ft 11 in`` become one length in feet. A bare number takes
    ``default_unit`` (noted as ``"unit-from-default"``); without one it is ``None``.
    """
    if not text:
        return None
    feet = _FEET_INCHES.search(text)
    if feet:
        value = D(feet.group(1)) + D(feet.group(2)) / 12
        return Quantity(value, "ft")
    for amount, _start, end, local in iter_numbers(text, allow_suffix=False):
        match = _SPELLING_RE.match(text, end)
        if match is not None and match.group(1).lower() in _WORDLIKE and _NEXT_WORD.match(text, match.end()):
            match = None  # "2 in stock", "21st century": a word, not a unit
        if match is not None:
            symbol = _symbol(match.group(1))
            if symbol is not None:
                for code in local:
                    _note(notes, code)
                return Quantity(amount, symbol)
        if default_unit is not None:
            symbol = _symbol(default_unit)
            if symbol is None:
                raise ConfigurationError(f"unknown unit {default_unit!r}", key="default_unit")
            for code in local:
                _note(notes, code)
            _note(notes, "unit-from-default")
            return Quantity(amount, symbol)
    return None


def parse_dimensions(text: str | None, *, notes: list[str] | None = None) -> list[Quantity]:
    """``"10 x 20 x 30 cm"`` -> three lengths in cm (a trailing unit applies to every number)."""
    if not text:
        return []
    parts = re.split(r"\s*[x×*]\s*", text.strip())
    if len(parts) < 2:
        single = parse_quantity(text, notes=notes)
        return [single] if single else []
    quantities = [parse_quantity(p) for p in parts]
    unit = next((q.unit for q in reversed(quantities) if q is not None), None)
    if unit is None:
        return []
    out = []
    for part, q in zip(parts, quantities, strict=True):
        if q is None:
            q = parse_quantity(part, default_unit=unit)
            if q is None:
                return []
        out.append(q)
    return out

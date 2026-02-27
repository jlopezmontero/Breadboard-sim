"""
Component geometry definitions.
All coordinates are (row_offset, col_offset) relative to anchor (0,0).
Pin 1 is always at anchor.
"""

import math


def _rotate_offset(row, col, rotation):
    """Rotate (row, col) offset by 0/90/180/270 degrees CW."""
    if rotation == 0:
        return (row, col)
    elif rotation == 90:
        return (col, -row)
    elif rotation == 180:
        return (-row, -col)
    elif rotation == 270:
        return (-col, row)
    return (row, col)


class ComponentDef:
    """Base component definition."""

    def __init__(self, type_id, name, ref_prefix, pins, body_cells, color, pin_labels=None):
        self.type_id = type_id      # e.g. "DIP-40", "RES-5"
        self.name = name            # e.g. "DIP-40 IC", "Resistor"
        self.ref_prefix = ref_prefix  # e.g. "U", "R", "C"
        self.pins = pins            # list of (row_off, col_off)
        self.body_cells = body_cells  # list of (row_off, col_off) - non-pin body cells
        self.color = color          # body fill color
        self.pin_labels = pin_labels  # optional dict: pin_index -> label

    def get_rotated_pins(self, rotation=0):
        return [_rotate_offset(r, c, rotation) for r, c in self.pins]

    def get_rotated_body(self, rotation=0):
        return [_rotate_offset(r, c, rotation) for r, c in self.body_cells]

    def get_bounds(self, rotation=0):
        """Return (min_row, min_col, max_row, max_col) of all cells."""
        all_cells = self.get_rotated_pins(rotation) + self.get_rotated_body(rotation)
        if not all_cells:
            return (0, 0, 0, 0)
        rows = [r for r, c in all_cells]
        cols = [c for r, c in all_cells]
        return (min(rows), min(cols), max(rows), max(cols))


def make_dip(pin_count, wide=False):
    """Create a DIP IC component definition.

    Narrow DIP: row_spacing=3 (pins in rows 0 and 3, body in rows 1-2)
    Wide DIP:   row_spacing=6 (pins in rows 0 and 6, body in rows 1-5)
    """
    if wide:
        row_spacing = 6
    else:
        row_spacing = 3

    half = pin_count // 2
    pins = []
    body_cells = []

    # Bottom row: pin 1..half left to right (counter-clockwise start)
    for i in range(half):
        pins.append((row_spacing, i))

    # Top row: pin half+1..pin_count right to left (counter-clockwise return)
    for i in range(half - 1, -1, -1):
        pins.append((0, i))

    # Body cells: rows between pin rows, all columns
    for r in range(1, row_spacing):
        for c in range(half):
            body_cells.append((r, c))

    suffix = "W" if wide else ""
    type_id = f"DIP-{pin_count}{suffix}"
    name = f"DIP-{pin_count}{' Wide' if wide else ''} IC"

    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix="U",
        pins=pins,
        body_cells=body_cells,
        color="#303030",
    )


def make_dip_custom(pin_count, row_spacing, type_id=None, name=None):
    """Create a DIP with arbitrary row spacing (distance between pin rows in pads).
    pin_count must be even. Pins per side = pin_count / 2 = number of columns."""
    half = pin_count // 2
    pins = []
    body_cells = []

    # Bottom row: pin 1..half left to right (counter-clockwise start)
    for i in range(half):
        pins.append((row_spacing, i))
    # Top row: pin half+1..pin_count right to left (counter-clockwise return)
    for i in range(half - 1, -1, -1):
        pins.append((0, i))
    for r in range(1, row_spacing):
        for c in range(half):
            body_cells.append((r, c))

    if type_id is None:
        type_id = f"DIP-{pin_count}-{half}x{row_spacing + 1}"
    if name is None:
        name = f"DIP-{pin_count} ({half}x{row_spacing + 1})"

    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix="U",
        pins=pins,
        body_cells=body_cells,
        color="#303030",
    )


def make_qfp(side_pins, type_id=None, name=None, color="#303030"):
    """Create a QFP component with pins on all 4 sides.
    side_pins: number of pins per side. Total = side_pins * 4.
    Footprint is (side_pins + 2) x (side_pins + 2): pins on the perimeter, body inside."""
    size = side_pins + 2  # outer dimension including pin row
    pins = []
    body_cells = []

    # Bottom side (row 0): left to right
    for c in range(1, side_pins + 1):
        pins.append((0, c))
    # Right side (col = size-1): top to bottom
    for r in range(1, side_pins + 1):
        pins.append((r, size - 1))
    # Top side (row = size-1): right to left
    for c in range(side_pins, 0, -1):
        pins.append((size - 1, c))
    # Left side (col 0): bottom to top
    for r in range(side_pins, 0, -1):
        pins.append((r, 0))

    # Body: interior cells + 4 corners
    for r in range(1, size - 1):
        for c in range(1, size - 1):
            body_cells.append((r, c))
    body_cells.append((0, 0))
    body_cells.append((0, size - 1))
    body_cells.append((size - 1, 0))
    body_cells.append((size - 1, size - 1))

    total = side_pins * 4
    if type_id is None:
        type_id = f"QFP-{total}"
    if name is None:
        name = f"QFP-{total} ({size}x{size})"

    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix="U",
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_plcc_adapter(pin_count, size, type_id=None, name=None, color="#2C3E50"):
    """Create a PLCC-to-PGA adapter socket footprint.
    Pins on outer ring(s) of a size x size grid, centered on each side
    when a ring is only partially filled."""
    pins = []

    def _ring_sides(ring_idx):
        s = ring_idx
        e = size - 1 - ring_idx
        top = [(s, c) for c in range(s, e + 1)]
        right = [(r, e) for r in range(s + 1, e + 1)]
        bottom = [(e, c) for c in range(e - 1, s - 1, -1)]
        left = [(r, s) for r in range(e - 1, s, -1)]
        return [top, right, bottom, left]

    ring_idx = 0
    while len(pins) < pin_count and ring_idx * 2 < size:
        sides = _ring_sides(ring_idx)
        all_ring = []
        for side in sides:
            all_ring.extend(side)
        if len(pins) + len(all_ring) <= pin_count:
            pins.extend(all_ring)
        else:
            # Partial ring: distribute evenly, centered on each side
            needed = pin_count - len(pins)
            per_side = needed // 4
            remainder = needed % 4
            for i, side in enumerate(sides):
                n = per_side + (1 if i < remainder else 0)
                start = (len(side) - n) // 2
                pins.extend(side[start:start + n])
            break
        ring_idx += 1

    pin_set = set(pins)
    body_cells = [(r, c) for r in range(size) for c in range(size)
                  if (r, c) not in pin_set]

    if type_id is None:
        type_id = f"PLCC-{pin_count}"
    if name is None:
        name = f"PLCC-{pin_count} Adapter ({size}x{size})"

    return ComponentDef(type_id, name, "U", pins, body_cells, color)


def make_axial(span=5, name="Resistor", ref_prefix="R", color="#C4A265"):
    """Create an axial component (resistor, diode). 2 pins separated by span-1 holes."""
    pins = [(0, 0), (0, span - 1)]
    body_cells = [(0, c) for c in range(1, span - 1)]
    type_id = f"AXIAL-{span}"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix=ref_prefix,
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_sip(pin_count, name=None, ref_prefix="RN", color="#4A148C"):
    """Create a SIP (Single In-line Package) component. All pins in a single row."""
    pins = [(0, c) for c in range(pin_count)]
    body_cells = []  # All cells are pins
    type_id = f"SIP-{pin_count}"
    if name is None:
        name = f"Resistor Array {pin_count}-pin"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix=ref_prefix,
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_radial_2pin(name="Capacitor", ref_prefix="C", color="#1565C0"):
    """Create a 2-pin radial component (cap, LED). Pins at (0,0) and (0,1)."""
    pins = [(0, 0), (0, 1)]
    body_cells = []
    type_id = "RADIAL-2"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix=ref_prefix,
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_electrolytic(name="Electrolytic Cap", ref_prefix="C", color="#0D47A1"):
    """Electrolytic capacitor: 2 pins at (0,0) and (1,0), body at (0,1) and (1,1)."""
    pins = [(0, 0), (1, 0)]
    body_cells = [(0, 1), (1, 1)]
    type_id = "ELEC-CAP"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix=ref_prefix,
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_crystal(name="Crystal HC49", ref_prefix="Y", color="#C0C0C0"):
    """HC49 crystal: 2 pins spaced 2 apart."""
    pins = [(0, 0), (0, 2)]
    body_cells = [(0, 1)]
    type_id = "HC49"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix=ref_prefix,
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_led(name="LED", color="#FF1744", type_suffix=""):
    """LED: 2-pin radial."""
    pins = [(0, 0), (0, 1)]
    body_cells = []
    type_id = f"LED{type_suffix}"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix="D",
        pins=pins,
        body_cells=body_cells,
        color=color,
    )


def make_header(rows_h, cols_h):
    """Create a pin header component (1xN or 2xN)."""
    pins = []
    for r in range(rows_h):
        for c in range(cols_h):
            pins.append((r, c))
    type_id = f"HDR-{rows_h}x{cols_h}"
    name = f"Header {rows_h}x{cols_h}"
    return ComponentDef(
        type_id=type_id,
        name=name,
        ref_prefix="J",
        pins=pins,
        body_cells=[],
        color="#222222",
    )


# Pre-built component catalog
BUILTIN_COMPONENTS = {}


def _register(comp):
    BUILTIN_COMPONENTS[comp.type_id] = comp


# DIP ICs
for n in [8, 14, 16, 20, 24, 28]:
    _register(make_dip(n))
_register(make_dip(24, wide=True))
_register(make_dip(28, wide=True))
_register(make_dip(32, wide=True))
_register(make_dip(40, wide=True))
# Large/custom DIPs
_register(make_dip_custom(60, 13, "DIP-60-DCJ11", "DCJ11 (30x14)"))   # 30 cols, 14 rows
_register(make_dip_custom(64, 9, "DIP-64-68000", "68000 (32x10)"))     # 32 cols, 10 rows
_register(make_dip_custom(40, 7, "DIP-40-PIPICO", "Pi Pico (20x8)"))   # 20 cols, 8 rows
_register(make_qfp(20, "QFP-144", "QFP-144 (22x22)"))                  # 20 pins/side, 22x22 footprint
_register(make_plcc_adapter(84, 15))                                   # PLCC-84 adapter, 15x15

# Passives
_register(make_axial(4, "Resistor (4h)", "R", "#C4A265"))
_register(make_axial(5, "Resistor (5h)", "R", "#C4A265"))
_register(make_radial_2pin("Capacitor (2h)", "C", "#1565C0"))
_register(ComponentDef("CAP-3H", "Capacitor (3h)", "C",
                        [(0, 0), (0, 2)], [(0, 1)], "#1565C0"))
_register(make_electrolytic("Electrolytic Cap (2h)"))
_register(ComponentDef("ELEC-CAP-3H", "Electrolytic Cap (3h)", "C",
                        [(0, 0), (0, 2)], [(1, 0), (1, 1), (1, 2), (0, 1)], "#0D47A1"))
_register(make_crystal())

# Resistor Arrays (SIP)
for n in [8, 9, 10]:
    _register(make_sip(n))

# Semiconductors
_register(make_led("LED Red", "#FF1744", "-RED"))
_register(make_led("LED Green", "#00C853", "-GRN"))

# Connectors
for n in range(2, 11):
    _register(make_header(1, n))
for n in [5, 10, 20, 40]:
    _register(make_header(2, n))

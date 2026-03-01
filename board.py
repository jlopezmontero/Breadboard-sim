"""
Board model: grid of pads, component placement, collision detection.
No GUI dependencies - pure data model.
"""

import math


def _point_to_segment_dist(pr, pc, r1, c1, r2, c2):
    """Distance from point (pr, pc) to line segment (r1,c1)-(r2,c2)."""
    dr = r2 - r1
    dc = c2 - c1
    len_sq = dr * dr + dc * dc
    if len_sq == 0:
        return math.hypot(pr - r1, pc - c1)
    t = max(0, min(1, ((pr - r1) * dr + (pc - c1) * dc) / len_sq))
    proj_r = r1 + t * dr
    proj_c = c1 + t * dc
    return math.hypot(pr - proj_r, pc - proj_c)


class TextLabel:
    """A free-floating text annotation on the board (no collision)."""

    def __init__(self, label_id, row, col, text=''):
        self.id = label_id
        self.row = float(row)
        self.col = float(col)
        self.text = text
        self.size = None          # font size override (int), None = auto
        self.align = 'center'     # 'left', 'center', 'right'
        self.opacity = 100        # background opacity 0-100
        self.layer = 'above'      # 'above' or 'below' components
        self.color = '#E0E0E0'    # text color
        self.rotation = 0         # 0, 90, 180, 270 degrees (CW)
        self.bg_color = '#000000' # background fill color
        self.border_color = ''    # border color, '' = no border

    def to_dict(self):
        d = {'id': self.id, 'row': self.row, 'col': self.col, 'text': self.text}
        if self.size is not None:
            d['size'] = self.size
        if self.align != 'center':
            d['align'] = self.align
        if self.opacity != 100:
            d['opacity'] = self.opacity
        if self.layer != 'above':
            d['layer'] = self.layer
        if self.color != '#E0E0E0':
            d['color'] = self.color
        if self.rotation != 0:
            d['rotation'] = self.rotation
        if self.bg_color != '#000000':
            d['bg_color'] = self.bg_color
        if self.border_color:
            d['border_color'] = self.border_color
        return d

    @classmethod
    def from_dict(cls, data):
        tl = cls(data['id'], data['row'], data['col'], data.get('text', ''))
        tl.size = data.get('size')
        tl.align = data.get('align', 'center')
        tl.opacity = data.get('opacity', 100)
        tl.layer = data.get('layer', 'above')
        tl.color = data.get('color', '#E0E0E0')
        tl.rotation = data.get('rotation', 0)
        tl.bg_color = data.get('bg_color', '#000000')
        tl.border_color = data.get('border_color', '')
        return tl


class Pad:
    """Single pad on the perfboard."""
    __slots__ = ('row', 'col', 'occupied_by')

    def __init__(self, row, col):
        self.row = row
        self.col = col
        self.occupied_by = None  # PlacedComponent.id or None


class PlacedComponent:
    """A component placed on the board."""

    def __init__(self, comp_id, comp_def, anchor_row, anchor_col, rotation=0):
        self.id = comp_id           # e.g. "U1", "R3"
        self.comp_def = comp_def    # ComponentDef reference
        self.anchor_row = anchor_row
        self.anchor_col = anchor_col
        self.rotation = rotation    # 0, 90, 180, 270
        self.label = None           # custom display name (e.g. "Z80"), None = show id
        self.label_size = None      # font size override (int), None = auto
        self.label_align = 'center' # text alignment: 'left', 'center', 'right'

    def get_occupied_cells(self):
        """Return list of (row, col) that block placement (pins only).
        Body cells are visual-only and don't block."""
        return set(self.get_pin_positions())

    def get_all_cells(self):
        """Return all cells (pins + body) for bounds checking and rendering."""
        cells = set()
        for r, c in self.comp_def.get_rotated_pins(self.rotation):
            cells.add((self.anchor_row + r, self.anchor_col + c))
        for r, c in self.comp_def.get_rotated_body(self.rotation):
            cells.add((self.anchor_row + r, self.anchor_col + c))
        return cells

    def get_pin_positions(self):
        """Return list of (row, col) for pins only."""
        return [(self.anchor_row + r, self.anchor_col + c)
                for r, c in self.comp_def.get_rotated_pins(self.rotation)]

    def get_body_cells(self):
        """Return list of (row, col) for body cells only."""
        return [(self.anchor_row + r, self.anchor_col + c)
                for r, c in self.comp_def.get_rotated_body(self.rotation)]

    def to_dict(self):
        d = {
            'id': self.id,
            'type': self.comp_def.type_id,
            'name': self.comp_def.name,
            'anchor_row': self.anchor_row,
            'anchor_col': self.anchor_col,
            'rotation': self.rotation,
        }
        if self.label:
            d['label'] = self.label
        if self.label_size is not None:
            d['label_size'] = self.label_size
        if self.label_align != 'center':
            d['label_align'] = self.label_align
        return d


class GuideLine:
    """A guide line between two pads."""
    __slots__ = ('r1', 'c1', 'r2', 'c2', 'color')

    def __init__(self, r1, c1, r2, c2, color='#FFFFFF'):
        self.r1 = r1
        self.c1 = c1
        self.r2 = r2
        self.c2 = c2
        self.color = color

    def to_dict(self):
        return [self.r1, self.c1, self.r2, self.c2, self.color]

    @classmethod
    def from_dict(cls, data):
        if len(data) >= 5:
            return cls(data[0], data[1], data[2], data[3], data[4])
        return cls(data[0], data[1], data[2], data[3])


class DivisionLine:
    """A division line between grid intersections (runs between pads, not through them).
    Coordinates are grid-edge points: (r, c) = top-left corner of cell (r, c).
    Valid range: r in [0, rows], c in [0, cols]."""
    __slots__ = ('r1', 'c1', 'r2', 'c2')

    def __init__(self, r1, c1, r2, c2):
        self.r1 = r1
        self.c1 = c1
        self.r2 = r2
        self.c2 = c2

    def to_dict(self):
        return [self.r1, self.c1, self.r2, self.c2]

    @classmethod
    def from_dict(cls, data):
        return cls(*data)


class Board:
    """Perfboard grid model."""

    def __init__(self, rows=57, cols=74):
        self.rows = rows
        self.cols = cols
        self.title = ""
        self.label_config = {}  # row_mode, col_mode, row_dir, col_dir
        self.pads = [[Pad(r, c) for c in range(cols)] for r in range(rows)]
        self.components = {}  # id -> PlacedComponent
        self.guides = []      # list of GuideLine
        self.divisions = []   # list of DivisionLine
        self.text_labels = [] # list of TextLabel
        self._next_counters = {}  # type_prefix -> next number
        self._next_text_label_num = 1

    def _in_bounds(self, row, col):
        return 0 <= row < self.rows and 0 <= col < self.cols

    def _generate_id(self, comp_def):
        """Generate next available ID like U1, U2, R1, etc."""
        prefix = comp_def.ref_prefix
        if prefix not in self._next_counters:
            self._next_counters[prefix] = 1
        while True:
            comp_id = f"{prefix}{self._next_counters[prefix]}"
            self._next_counters[prefix] += 1
            if comp_id not in self.components:
                return comp_id

    def _count_components_at(self, row, col, exclude_id=None):
        """Count how many components have a cell (pin or body) at (row, col)."""
        count = 0
        occ = self.pads[row][col].occupied_by
        if occ is not None and occ != exclude_id:
            count += 1
        for pc in self.components.values():
            if pc.id == exclude_id or pc.id == occ:
                continue
            for br, bc in pc.get_body_cells():
                if br == row and bc == col:
                    count += 1
                    break
        return count

    def can_place(self, comp_def, anchor_row, anchor_col, rotation=0, exclude_id=None):
        """Check if component can be placed at given position.
        Only PINS block each other. Body cells just need to be in bounds.
        Max 2 components per cell (no triple overlap)."""
        pins = comp_def.get_rotated_pins(rotation)
        body = comp_def.get_rotated_body(rotation)
        for r, c in pins:
            ar, ac = anchor_row + r, anchor_col + c
            if not self._in_bounds(ar, ac):
                return False
            occupant = self.pads[ar][ac].occupied_by
            if occupant is not None and occupant != exclude_id:
                return False
            if self._count_components_at(ar, ac, exclude_id) >= 2:
                return False
        for r, c in body:
            ar, ac = anchor_row + r, anchor_col + c
            if not self._in_bounds(ar, ac):
                return False
            if self._count_components_at(ar, ac, exclude_id) >= 2:
                return False
        return True

    def place_component(self, comp_def, anchor_row, anchor_col, rotation=0, comp_id=None):
        """Place component. Returns PlacedComponent or None if invalid."""
        if not self.can_place(comp_def, anchor_row, anchor_col, rotation):
            return None
        if comp_id is None:
            comp_id = self._generate_id(comp_def)
        pc = PlacedComponent(comp_id, comp_def, anchor_row, anchor_col, rotation)
        self.components[comp_id] = pc
        for r, c in pc.get_occupied_cells():
            self.pads[r][c].occupied_by = comp_id
        return pc

    def remove_component(self, comp_id):
        """Remove component from board. Returns the PlacedComponent or None."""
        if comp_id not in self.components:
            return None
        pc = self.components.pop(comp_id)
        for r, c in pc.get_occupied_cells():
            if self._in_bounds(r, c) and self.pads[r][c].occupied_by == comp_id:
                self.pads[r][c].occupied_by = None
        return pc

    def move_component(self, comp_id, new_row, new_col):
        """Move component to new anchor position. Returns True on success."""
        if comp_id not in self.components:
            return False
        pc = self.components[comp_id]
        if not self.can_place(pc.comp_def, new_row, new_col, pc.rotation, exclude_id=comp_id):
            return False
        # Clear old cells
        for r, c in pc.get_occupied_cells():
            if self._in_bounds(r, c) and self.pads[r][c].occupied_by == comp_id:
                self.pads[r][c].occupied_by = None
        # Update position
        pc.anchor_row = new_row
        pc.anchor_col = new_col
        # Mark new cells
        for r, c in pc.get_occupied_cells():
            self.pads[r][c].occupied_by = comp_id
        return True

    def rotate_component(self, comp_id):
        """Rotate component 90 degrees CW. Returns (True, new_row, new_col) or (False, None, None).
        If rotation at current anchor doesn't fit, tries shifting anchor to keep in bounds."""
        if comp_id not in self.components:
            return (False, None, None)
        pc = self.components[comp_id]
        new_rot = (pc.rotation + 90) % 360
        ar, ac = pc.anchor_row, pc.anchor_col

        # Try current position first, then shifted positions
        target = self._find_rotate_position(pc, new_rot)
        if target is None:
            return (False, None, None)

        new_ar, new_ac = target
        # Clear old cells
        for r, c in pc.get_occupied_cells():
            if self._in_bounds(r, c) and self.pads[r][c].occupied_by == comp_id:
                self.pads[r][c].occupied_by = None
        pc.anchor_row = new_ar
        pc.anchor_col = new_ac
        pc.rotation = new_rot
        # Mark new cells
        for r, c in pc.get_occupied_cells():
            self.pads[r][c].occupied_by = comp_id
        return (True, new_ar, new_ac)

    def _find_rotate_position(self, pc, new_rot):
        """Find a valid anchor for rotation, shifting if needed to stay in bounds."""
        ar, ac = pc.anchor_row, pc.anchor_col
        comp_id = pc.id

        # Try in-place first
        if self.can_place(pc.comp_def, ar, ac, new_rot, exclude_id=comp_id):
            return (ar, ac)

        # Calculate how far out of bounds the rotated component would be
        all_offsets = pc.comp_def.get_rotated_pins(new_rot) + pc.comp_def.get_rotated_body(new_rot)
        if not all_offsets:
            return None

        min_r = min(r for r, c in all_offsets)
        max_r = max(r for r, c in all_offsets)
        min_c = min(c for r, c in all_offsets)
        max_c = max(c for r, c in all_offsets)

        # Shift needed to bring all cells within bounds
        shift_r = 0
        shift_c = 0
        if ar + min_r < 0:
            shift_r = -(ar + min_r)
        elif ar + max_r >= self.rows:
            shift_r = (self.rows - 1) - (ar + max_r)
        if ac + min_c < 0:
            shift_c = -(ac + min_c)
        elif ac + max_c >= self.cols:
            shift_c = (self.cols - 1) - (ac + max_c)

        new_ar = ar + shift_r
        new_ac = ac + shift_c
        if self.can_place(pc.comp_def, new_ar, new_ac, new_rot, exclude_id=comp_id):
            return (new_ar, new_ac)

        return None

    def get_component_at(self, row, col):
        """Return PlacedComponent at given pad, or None.
        Checks pins first (via pad grid), then body cells.
        When overlapping, prefers the smaller (inner) component."""
        if not self._in_bounds(row, col):
            return None
        # Check pins (fast path via occupancy grid)
        occ = self.pads[row][col].occupied_by
        if occ is not None:
            return self.components.get(occ)
        # Check body cells - prefer smallest component (inner over outer)
        best = None
        best_size = float('inf')
        for pc in self.components.values():
            if (row, col) in pc.get_all_cells():
                size = len(pc.comp_def.pins) + len(pc.comp_def.body_cells)
                if size < best_size:
                    best = pc
                    best_size = size
        return best

    def add_guide(self, r1, c1, r2, c2, color='#FFFFFF'):
        """Add a guide line between two pads. Returns the GuideLine."""
        gl = GuideLine(r1, c1, r2, c2, color)
        self.guides.append(gl)
        return gl

    def remove_guide(self, index):
        """Remove guide line by index. Returns the removed GuideLine or None."""
        if 0 <= index < len(self.guides):
            return self.guides.pop(index)
        return None

    def find_guide_near(self, row, col, tolerance=0.35):
        """Find guide line closest to (row, col). Returns index or -1."""
        best_idx = -1
        best_dist = float('inf')
        for i, gl in enumerate(self.guides):
            d = _point_to_segment_dist(row, col, gl.r1, gl.c1, gl.r2, gl.c2)
            if d < best_dist and d <= tolerance:
                best_dist = d
                best_idx = i
        return best_idx

    def add_division(self, r1, c1, r2, c2):
        """Add a division line between grid intersections. Returns the DivisionLine."""
        dl = DivisionLine(r1, c1, r2, c2)
        self.divisions.append(dl)
        return dl

    def remove_division(self, index):
        """Remove division line by index. Returns the removed DivisionLine or None."""
        if 0 <= index < len(self.divisions):
            return self.divisions.pop(index)
        return None

    def find_division_near(self, row, col, tolerance=0.3):
        """Find division line closest to grid-edge point (row, col). Returns index or -1."""
        best_idx = -1
        best_dist = float('inf')
        for i, dl in enumerate(self.divisions):
            d = _point_to_segment_dist(row, col, dl.r1, dl.c1, dl.r2, dl.c2)
            if d < best_dist and d <= tolerance:
                best_dist = d
                best_idx = i
        return best_idx

    def clear(self):
        """Remove all components, guides, divisions and text labels."""
        for r in range(self.rows):
            for c in range(self.cols):
                self.pads[r][c].occupied_by = None
        self.components.clear()
        self.guides.clear()
        self.divisions.clear()
        self.text_labels.clear()
        self._next_counters.clear()
        self._next_text_label_num = 1

    def add_text_label(self, row, col, text=''):
        tl_id = f'T{self._next_text_label_num}'
        self._next_text_label_num += 1
        tl = TextLabel(tl_id, row, col, text)
        self.text_labels.append(tl)
        return tl

    def remove_text_label(self, label_id):
        self.text_labels = [tl for tl in self.text_labels if tl.id != label_id]

    def get_text_label(self, label_id):
        for tl in self.text_labels:
            if tl.id == label_id:
                return tl
        return None

    def resize(self, new_rows, new_cols):
        """Resize board. Removes components that would be out of bounds."""
        to_remove = []
        for comp_id, pc in self.components.items():
            for r, c in pc.get_all_cells():
                if r >= new_rows or c >= new_cols:
                    to_remove.append(comp_id)
                    break
        for comp_id in to_remove:
            self.remove_component(comp_id)

        old_rows, old_cols = self.rows, self.cols
        self.rows = new_rows
        self.cols = new_cols
        new_pads = [[Pad(r, c) for c in range(new_cols)] for r in range(new_rows)]
        for r in range(min(old_rows, new_rows)):
            for c in range(min(old_cols, new_cols)):
                new_pads[r][c].occupied_by = self.pads[r][c].occupied_by
        self.pads = new_pads

    def rotate_board_cw(self):
        """Rotate entire board 90 degrees clockwise, including all components and guides.
        Board dimensions swap: (rows, cols) -> (cols, rows).
        Each component anchor transforms and gains +90 rotation."""
        old_rows = self.rows
        # Save component state
        comp_data = []
        for pc in self.components.values():
            comp_data.append((
                pc.id, pc.comp_def,
                pc.anchor_row, pc.anchor_col, pc.rotation,
                pc.label, pc.label_size, pc.label_align,
            ))
        # Save guide and division state
        guide_data = [(gl.r1, gl.c1, gl.r2, gl.c2, gl.color) for gl in self.guides]
        div_data = [(dl.r1, dl.c1, dl.r2, dl.c2) for dl in self.divisions]

        # Clear and rebuild with swapped dimensions
        self.clear()
        new_rows, new_cols = self.cols, self.rows
        self.rows = new_rows
        self.cols = new_cols
        self.pads = [[Pad(r, c) for c in range(new_cols)] for r in range(new_rows)]

        # Re-place with transformed coordinates
        for comp_id, comp_def, ar, ac, rot, label, label_size, label_align in comp_data:
            new_ar = ac
            new_ac = (old_rows - 1) - ar
            new_rot = (rot + 90) % 360
            placed = self.place_component(comp_def, new_ar, new_ac, new_rot, comp_id=comp_id)
            if placed:
                if label is not None:
                    placed.label = label
                if label_size is not None:
                    placed.label_size = label_size
                placed.label_align = label_align

        # Re-add guides with transformed coordinates
        for r1, c1, r2, c2, color in guide_data:
            self.add_guide(c1, (old_rows - 1) - r1, c2, (old_rows - 1) - r2, color)

        # Re-add divisions with transformed coordinates
        # Division coords are grid-edge: range [0, old_rows] x [0, old_cols]
        for r1, c1, r2, c2 in div_data:
            self.add_division(c1, old_rows - r1, c2, old_rows - r2)

    def to_dict(self):
        d = {
            'rows': self.rows,
            'cols': self.cols,
            'title': self.title,
        }
        if self.label_config:
            d['label_config'] = self.label_config
        return d

    @classmethod
    def from_dict(cls, data):
        board = cls(data['rows'], data['cols'])
        board.title = data.get('title', '')
        board.label_config = data.get('label_config', {})
        return board

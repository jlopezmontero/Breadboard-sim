"""
Canvas renderer: draws pads, components, ghost preview, handles zoom/pan.
Full redraw on zoom for pixel-perfect quality (no canvas.scale()).
"""

import tkinter as tk


# Colors
COLOR_PCB_BG = '#1B5E20'
COLOR_PAD = '#C0C0C0'
COLOR_PAD_OCCUPIED = '#A0A060'
COLOR_PIN_GOLD = '#DAA520'
COLOR_GHOST_VALID = '#00E676'
COLOR_GHOST_INVALID = '#FF1744'
COLOR_SELECTION = '#00B0FF'
COLOR_GRID_LINE = '#2E7D32'
COLOR_TEXT = '#E0E0E0'
COLOR_PIN1_MARK = '#FFFFFF'
COLOR_GUIDE = '#FFFFFF'
COLOR_GUIDE_PREVIEW = '#AAAAAA'
COLOR_DIVISION = '#4CAF50'
COLOR_DIVISION_PREVIEW = '#81C784'
COLOR_DIVISION_SELECTED = '#A5D6A7'
COLOR_LABEL = '#CCCCCC'

# Base cell size in pixels at zoom=1.0
BASE_CELL = 16
MIN_ZOOM = 0.25
MAX_ZOOM = 4.0


class BoardRenderer:
    """Renders the board onto a tkinter Canvas."""

    def __init__(self, canvas, board):
        self.canvas = canvas
        self.board = board
        self.zoom = 1.0
        self.offset_x = 0.0  # canvas pixel offset for panning
        self.offset_y = 0.0
        self.ghost = None        # (comp_def, row, col, rotation, valid)
        self.guide_preview = None     # (r1, c1, r2, c2) while drawing a guide
        self.division_preview = None  # (r1, c1, r2, c2) while drawing a division
        self.selected_division = -1  # index of selected division, or -1
        self.selected_ids = set()  # highlighted component ids
        self.selection_rect = None    # (x0,y0,x1,y1) canvas coords for rubber-band, or None
        self.multi_ghost = None       # list of (comp_def, row, col, rot, valid) or None
        self.show_labels = True
        self.show_grid = True
        self.selected_text_label_id = None  # id of selected free text label
        self._text_label_bboxes = {}        # id -> (x0,y0,x1,y1) canvas bbox
        self.text_label_ghost = None        # dict of props when placing a new text label
        self.text_label_ghost_pos = (0.0, 0.0)  # (row_f, col_f) cursor position
        # Label modes: 'num' (1,2,3...) or 'alpha' (A,B,C...)
        self.row_label_mode = 'num'
        self.col_label_mode = 'num'
        # Label directions: 'asc' (top-to-bottom / left-to-right) or 'desc' (reversed)
        self.row_label_dir = 'asc'
        self.col_label_dir = 'asc'

        # Pan state
        self._pan_start_x = 0
        self._pan_start_y = 0

    @staticmethod
    def _grid_label(index, total, mode, direction):
        """Generate grid label for a row/col index.
        mode: 'num' (1-based) or 'alpha' (A, B, ... Z, AA, AB, ...)
        direction: 'asc' or 'desc'"""
        i = index if direction == 'asc' else (total - 1 - index)
        if mode == 'alpha':
            # A..Z, AA..AZ, BA..BZ, ...
            result = ''
            n = i
            while True:
                result = chr(ord('A') + n % 26) + result
                n = n // 26 - 1
                if n < 0:
                    break
            return result
        else:
            return str(i + 1)

    @property
    def cell(self):
        return BASE_CELL * self.zoom

    def grid_to_canvas(self, row, col):
        """Convert grid (row, col) to canvas pixel (x, y) center of pad."""
        x = self.offset_x + (col + 0.5) * self.cell
        y = self.offset_y + (row + 0.5) * self.cell
        return x, y

    def canvas_to_grid(self, cx, cy):
        """Convert canvas pixel to nearest grid (row, col) pad center."""
        col = (cx - self.offset_x) / self.cell - 0.5
        row = (cy - self.offset_y) / self.cell - 0.5
        return round(row), round(col)

    def canvas_to_grid_float(self, cx, cy):
        """Convert canvas pixel to grid (row, col) without rounding."""
        col = (cx - self.offset_x) / self.cell - 0.5
        row = (cy - self.offset_y) / self.cell - 0.5
        return row, col

    def edge_to_canvas(self, row, col):
        """Convert grid-edge intersection (row, col) to canvas pixel.
        Edge (0,0) = top-left corner of pad (0,0)."""
        x = self.offset_x + col * self.cell
        y = self.offset_y + row * self.cell
        return x, y

    def canvas_to_edge(self, cx, cy):
        """Convert canvas pixel to nearest grid-edge intersection."""
        col = (cx - self.offset_x) / self.cell
        row = (cy - self.offset_y) / self.cell
        return round(row), round(col)

    def canvas_to_edge_float(self, cx, cy):
        """Convert canvas pixel to grid-edge coordinates without rounding."""
        col = (cx - self.offset_x) / self.cell
        row = (cy - self.offset_y) / self.cell
        return row, col

    def set_zoom(self, new_zoom, center_cx=None, center_cy=None):
        """Set zoom level, keeping center_cx/cy fixed on screen."""
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, new_zoom))
        if center_cx is None:
            center_cx = float(self.canvas.winfo_width()) / 2
            center_cy = float(self.canvas.winfo_height()) / 2

        # Grid position under cursor before zoom
        grid_x = (center_cx - self.offset_x) / self.cell
        grid_y = (center_cy - self.offset_y) / self.cell

        self.zoom = new_zoom
        cell = self.cell

        # Adjust offset so same grid point stays under cursor
        self.offset_x = center_cx - grid_x * cell
        self.offset_y = center_cy - grid_y * cell
        self.redraw()

    def zoom_in(self, cx=None, cy=None):
        self.set_zoom(self.zoom * 1.2, cx, cy)

    def zoom_out(self, cx=None, cy=None):
        self.set_zoom(self.zoom / 1.2, cx, cy)

    def zoom_fit(self):
        """Fit entire board in canvas with margin on all sides."""
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        # Extra margin: labels on left/top, breathing room on right/bottom
        margin_cells = 2 if self.show_labels else 1
        zx = cw / ((self.board.cols + margin_cells * 2) * BASE_CELL)
        zy = ch / ((self.board.rows + margin_cells * 2) * BASE_CELL)
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, min(zx, zy)))
        cell = self.cell
        total_w = self.board.cols * cell
        total_h = self.board.rows * cell
        # Shift towards right/bottom to leave more room for labels on left/top
        label_bias = cell * 0.5 if self.show_labels else 0
        self.offset_x = (cw - total_w) / 2 + label_bias
        self.offset_y = (ch - total_h) / 2 + label_bias
        self.redraw()

    def start_pan(self, event):
        self._pan_start_x = event.x
        self._pan_start_y = event.y

    def do_pan(self, event):
        dx = event.x - self._pan_start_x
        dy = event.y - self._pan_start_y
        self._pan_start_x = event.x
        self._pan_start_y = event.y
        self.offset_x += dx
        self.offset_y += dy
        self.redraw()

    def redraw(self):
        """Full redraw of the board."""
        self.canvas.delete('all')
        cell = self.cell
        pad_r = cell * 0.25

        # Board background
        x0 = self.offset_x
        y0 = self.offset_y
        x1 = x0 + self.board.cols * cell
        y1 = y0 + self.board.rows * cell
        self.canvas.create_rectangle(x0, y0, x1, y1, fill=COLOR_PCB_BG, outline='')

        # Grid lines (subtle)
        if self.show_grid and cell >= 8:
            for c in range(self.board.cols + 1):
                lx = x0 + c * cell
                self.canvas.create_line(lx, y0, lx, y1, fill=COLOR_GRID_LINE, width=1)
            for r in range(self.board.rows + 1):
                ly = y0 + r * cell
                self.canvas.create_line(x0, ly, x1, ly, fill=COLOR_GRID_LINE, width=1)

        # Division lines (between pads, on grid edges)
        div_width = max(3, cell * 0.25)
        for i, dl in enumerate(self.board.divisions):
            x1d, y1d = self.edge_to_canvas(dl.r1, dl.c1)
            x2d, y2d = self.edge_to_canvas(dl.r2, dl.c2)
            color = COLOR_DIVISION_SELECTED if i == self.selected_division else COLOR_DIVISION
            w = div_width * 1.5 if i == self.selected_division else div_width
            self.canvas.create_line(x1d, y1d, x2d, y2d,
                                    fill=color, width=w, capstyle='round')

        # Division preview (while drawing or dragging)
        if self.division_preview:
            dr1, dc1, dr2, dc2 = self.division_preview
            x1d, y1d = self.edge_to_canvas(dr1, dc1)
            x2d, y2d = self.edge_to_canvas(dr2, dc2)
            self.canvas.create_line(x1d, y1d, x2d, y2d,
                                    fill=COLOR_DIVISION_PREVIEW, width=div_width * 1.5,
                                    capstyle='round', dash=(6, 3))

        # Pads
        for r in range(self.board.rows):
            for c in range(self.board.cols):
                px, py = self.grid_to_canvas(r, c)
                occ = self.board.pads[r][c].occupied_by
                color = COLOR_PAD_OCCUPIED if occ else COLOR_PAD
                self.canvas.create_oval(
                    px - pad_r, py - pad_r, px + pad_r, py + pad_r,
                    fill=color, outline='#888888', width=max(1, cell * 0.05)
                )

        # Guide lines
        guide_width = max(2, cell * 0.15)
        for gl in self.board.guides:
            x1g, y1g = self.grid_to_canvas(gl.r1, gl.c1)
            x2g, y2g = self.grid_to_canvas(gl.r2, gl.c2)
            self.canvas.create_line(x1g, y1g, x2g, y2g,
                                    fill=gl.color, width=guide_width, capstyle='round')

        # Guide preview (while drawing)
        if self.guide_preview:
            gr1, gc1, gr2, gc2 = self.guide_preview
            x1g, y1g = self.grid_to_canvas(gr1, gc1)
            x2g, y2g = self.grid_to_canvas(gr2, gc2)
            self.canvas.create_line(x1g, y1g, x2g, y2g,
                                    fill=COLOR_GUIDE_PREVIEW, width=guide_width,
                                    capstyle='round', dash=(4, 4))

        # Text labels below components
        self._text_label_bboxes.clear()
        for tl in self.board.text_labels:
            if tl.layer == 'below':
                self._draw_text_label(tl)

        # Components - layered rendering
        inner_ids = self._find_inner_components()
        # Pass 1: All bodies (semi-transparent)
        for comp_id, pc in self.board.components.items():
            self._draw_component_body(pc, highlight=(comp_id in self.selected_ids))
        # Pass 2: Inner component bodies redrawn opaque + all pins/labels
        for comp_id, pc in self.board.components.items():
            self._draw_component_pins(pc, highlight=(comp_id in self.selected_ids),
                                      is_inner=(comp_id in inner_ids))

        # Guide segments hidden under component bodies (stippled overlay)
        self._draw_hidden_guides(guide_width)

        # Text labels above components
        for tl in self.board.text_labels:
            if tl.layer == 'above':
                self._draw_text_label(tl)

        # Rubber-band selection rectangle
        if self.selection_rect:
            rx0, ry0, rx1, ry1 = self.selection_rect
            self.canvas.create_rectangle(rx0, ry0, rx1, ry1,
                outline='#00B0FF', fill='#00B0FF', stipple='gray25',
                width=1, dash=(4, 3))

        # Ghost preview (multi or single)
        if self.multi_ghost:
            for comp_def, row, col, rot, valid in self.multi_ghost:
                self._draw_ghost(comp_def, row, col, rot, valid)
        elif self.ghost:
            self._draw_ghost(*self.ghost)

        # Text label ghost (new label being positioned)
        if self.text_label_ghost:
            from board import TextLabel as _TL
            _g = self.text_label_ghost
            tg = _TL('__ghost__', self.text_label_ghost_pos[0], self.text_label_ghost_pos[1],
                     _g.get('text', ''))
            tg.size = _g.get('size')
            tg.align = _g.get('align', 'center')
            tg.opacity = _g.get('opacity', 100)
            tg.layer = 'above'
            tg.color = _g.get('color', '#E0E0E0')
            tg.bg_color = _g.get('bg_color', '#000000')
            tg.border_color = _g.get('border_color', '')
            tg.rotation = _g.get('rotation', 0)
            self._draw_text_label(tg)
            bbox = self._text_label_bboxes.get('__ghost__')
            if bbox:
                bx0, by0, bx1, by1 = bbox
                self.canvas.create_rectangle(bx0, by0, bx1, by1,
                    outline='#00B0FF', fill='', width=2, dash=(4, 3))

        # Row/col labels
        if self.show_labels and cell >= 10:
            font_size = max(6, int(cell * 0.4))
            font = ('Consolas', font_size)
            for r in range(self.board.rows):
                lx = x0 - cell * 0.3
                ly = y0 + (r + 0.5) * cell
                if r % 5 == 0:
                    label = self._grid_label(r, self.board.rows, self.row_label_mode, self.row_label_dir)
                    self.canvas.create_text(lx, ly, text=label, fill=COLOR_LABEL,
                                            font=font, anchor='e')
            for c in range(self.board.cols):
                lx = x0 + (c + 0.5) * cell
                ly = y0 - cell * 0.3
                if c % 5 == 0:
                    label = self._grid_label(c, self.board.cols, self.col_label_mode, self.col_label_dir)
                    self.canvas.create_text(lx, ly, text=label, fill=COLOR_LABEL,
                                            font=font, anchor='s')

    @staticmethod
    def _opacity_to_stipple(opacity):
        """Map 0-100 background opacity to a Tkinter stipple string (or '' for solid)."""
        if opacity <= 0:
            return None      # no background drawn
        if opacity <= 20:
            return 'gray12'
        if opacity <= 40:
            return 'gray25'
        if opacity <= 60:
            return 'gray50'
        if opacity <= 80:
            return 'gray75'
        return ''            # solid

    def _draw_text_label(self, tl):
        """Draw a free TextLabel on the canvas and record its bbox."""
        cell = self.cell
        x, y = self.grid_to_canvas(tl.row, tl.col)
        font_size = tl.size or max(9, int(cell * 0.7))
        # Tkinter text angle: CCW degrees. Our rotation is CW degrees.
        tk_angle = (360 - tl.rotation) % 360

        tid = self.canvas.create_text(
            x, y, text=tl.text, fill=tl.color,
            font=('Consolas', font_size, 'bold'),
            justify=tl.align, anchor='center', angle=tk_angle,
        )
        bbox = self.canvas.bbox(tid)
        if bbox:
            pad = max(3, cell * 0.15)
            bx0, by0, bx1, by1 = bbox[0]-pad, bbox[1]-pad, bbox[2]+pad, bbox[3]+pad
            stipple = self._opacity_to_stipple(tl.opacity)
            if stipple is not None:
                kw = {'fill': tl.bg_color, 'outline': ''}
                if stipple:
                    kw['stipple'] = stipple
                self.canvas.create_rectangle(bx0, by0, bx1, by1, **kw)
            # Border
            if tl.border_color:
                self.canvas.create_rectangle(bx0, by0, bx1, by1,
                    outline=tl.border_color, fill='', width=1)
            # Selection highlight
            if tl.id == self.selected_text_label_id:
                self.canvas.create_rectangle(bx0, by0, bx1, by1,
                    outline='#00B0FF', fill='', width=2)
            self.canvas.tag_raise(tid)
            self._text_label_bboxes[tl.id] = (bx0, by0, bx1, by1)

    def _draw_component_body(self, pc, highlight=False):
        """Draw component body (semi-transparent, rendered first so pins go on top)."""
        cell = self.cell
        comp_def = pc.comp_def
        body_cells = pc.get_body_cells()
        if body_cells:
            rows_b = [r for r, c in body_cells]
            cols_b = [c for r, c in body_cells]
            min_r, max_r = min(rows_b), max(rows_b)
            min_c, max_c = min(cols_b), max(cols_b)
            bx0, by0 = self.grid_to_canvas(min_r, min_c)
            bx1, by1 = self.grid_to_canvas(max_r, max_c)
            margin = cell * 0.45
            outline_color = COLOR_SELECTION if highlight else '#555555'
            outline_w = 3 if highlight else 1
            self.canvas.create_rectangle(
                bx0 - margin, by0 - margin, bx1 + margin, by1 + margin,
                fill=comp_def.color, outline=outline_color, width=outline_w,
                stipple='gray75'
            )

    def _find_inner_components(self):
        """Find components placed underneath another component's body.
        A component is 'inner' only if its PINS are inside another component's BODY."""
        body_map = {}
        for comp_id, pc in self.board.components.items():
            for r, c in pc.get_body_cells():
                body_map.setdefault((r, c), set()).add(comp_id)
        inner = set()
        for comp_id, pc in self.board.components.items():
            for r, c in pc.get_pin_positions():
                owners = body_map.get((r, c), set())
                if owners - {comp_id}:
                    inner.add(comp_id)
                    break
        return inner

    def _draw_component_pins(self, pc, highlight=False, is_inner=False):
        """Draw component pins, labels, and selection (rendered on top of all bodies)."""
        cell = self.cell
        comp_def = pc.comp_def
        pin_r = cell * 0.3
        is_dip = comp_def.type_id.startswith('DIP-')

        # Inner component: redraw body opaque on top of stippled outer bodies
        if is_inner:
            body_cells = pc.get_body_cells()
            if body_cells:
                rows_b = [r for r, c in body_cells]
                cols_b = [c for r, c in body_cells]
                min_r, max_r = min(rows_b), max(rows_b)
                min_c, max_c = min(cols_b), max(cols_b)
                bx0, by0 = self.grid_to_canvas(min_r, min_c)
                bx1, by1 = self.grid_to_canvas(max_r, max_c)
                margin = cell * 0.45
                self.canvas.create_rectangle(
                    bx0 - margin, by0 - margin, bx1 + margin, by1 + margin,
                    fill=comp_def.color, outline='#555555', width=1
                )

        # Pins
        pin_positions = pc.get_pin_positions()
        for i, (pr, pcc) in enumerate(pin_positions):
            px, py = self.grid_to_canvas(pr, pcc)
            self.canvas.create_oval(
                px - pin_r, py - pin_r, px + pin_r, py + pin_r,
                fill=COLOR_PIN_GOLD, outline='#B8860B', width=max(1, cell * 0.06)
            )
            # Pin 1 mark
            if i == 0 and is_dip:
                dot_r = cell * 0.1
                self.canvas.create_oval(
                    px - dot_r, py - dot_r, px + dot_r, py + dot_r,
                    fill=COLOR_PIN1_MARK, outline=''
                )

        # Reference label
        if self.show_labels and cell >= 10:
            # Place label at center of component
            all_cells = list(pc.get_all_cells())
            if all_cells:
                avg_r = sum(r for r, c in all_cells) / len(all_cells)
                avg_c = sum(c for r, c in all_cells) / len(all_cells)
                lx, ly = self.grid_to_canvas(avg_r, avg_c)
                display_text = pc.label or pc.id
                rows_span = max(r for r, c in all_cells) - min(r for r, c in all_cells) + 1
                cols_span = max(c for r, c in all_cells) - min(c for r, c in all_cells) + 1
                min_span = min(rows_span, cols_span)
                if pc.label_size:
                    font_size = pc.label_size
                elif min_span <= 2:
                    font_size = max(6, int(cell * 0.4))
                else:
                    font_size = max(9, int(cell * 0.7))
                text_angle = 90 if pc.rotation in (90, 270) else 0
                tid = self.canvas.create_text(
                    lx, ly, text=display_text, fill=COLOR_TEXT,
                    font=('Consolas', font_size, 'bold'), angle=text_angle,
                    justify=getattr(pc, 'label_align', 'center'),
                )
                # Opaque background behind text for readability
                bbox = self.canvas.bbox(tid)
                if bbox:
                    pad = cell * 0.1
                    self.canvas.create_rectangle(
                        bbox[0] - pad, bbox[1] - pad,
                        bbox[2] + pad, bbox[3] + pad,
                        fill='#000000', outline=''
                    )
                    self.canvas.tag_raise(tid)

        # Selection highlight border (around all cells for bodyless components)
        body_cells = pc.get_body_cells()
        if highlight and not body_cells:
            for pr, pcc in pin_positions:
                px, py = self.grid_to_canvas(pr, pcc)
                m = cell * 0.4
                self.canvas.create_rectangle(
                    px - m, py - m, px + m, py + m,
                    outline=COLOR_SELECTION, width=2, fill=''
                )

    def _draw_hidden_guides(self, guide_width):
        """Draw guide segments hidden under component bodies with stipple overlay."""
        body_cells = set()
        for pc in self.board.components.values():
            for r, c in pc.get_body_cells():
                body_cells.add((r, c))
        if not body_cells:
            return
        for gl in self.board.guides:
            r1, c1, r2, c2 = gl.r1, gl.c1, gl.r2, gl.c2
            dr, dc = r2 - r1, c2 - c1
            steps = max(abs(dr), abs(dc))
            if steps == 0:
                continue
            # Walk integer grid points along the guide
            points = []
            for s in range(steps + 1):
                pr = r1 + dr * s // steps
                pc = c1 + dc * s // steps
                points.append((pr, pc))
            # Find contiguous runs of hidden points and draw them
            i = 0
            while i < len(points):
                if points[i] in body_cells:
                    j = i
                    while j < len(points) and points[j] in body_cells:
                        j += 1
                    sr, sc = points[i]
                    er, ec = points[j - 1]
                    x1, y1 = self.grid_to_canvas(sr, sc)
                    x2, y2 = self.grid_to_canvas(er, ec)
                    if (sr, sc) == (er, ec):
                        hw = guide_width / 2
                        self.canvas.create_oval(
                            x1 - hw, y1 - hw, x1 + hw, y1 + hw,
                            fill=gl.color, outline='', stipple='gray50')
                    else:
                        self.canvas.create_line(
                            x1, y1, x2, y2, fill=gl.color,
                            width=guide_width, capstyle='round',
                            stipple='gray50')
                    i = j
                else:
                    i += 1

    def _draw_ghost(self, comp_def, row, col, rotation, valid):
        """Draw transparent ghost of component being placed."""
        cell = self.cell
        color = COLOR_GHOST_VALID if valid else COLOR_GHOST_INVALID

        pins = comp_def.get_rotated_pins(rotation)
        body = comp_def.get_rotated_body(rotation)

        for r_off, c_off in pins:
            px, py = self.grid_to_canvas(row + r_off, col + c_off)
            pr = cell * 0.35
            self.canvas.create_oval(
                px - pr, py - pr, px + pr, py + pr,
                fill=color, outline='', stipple='gray50'
            )

        for r_off, c_off in body:
            px, py = self.grid_to_canvas(row + r_off, col + c_off)
            m = cell * 0.45
            self.canvas.create_rectangle(
                px - m, py - m, px + m, py + m,
                fill=color, outline='', stipple='gray50'
            )

    def export_png(self, filepath):
        """Export canvas as PNG using screen capture, or PostScript as fallback."""
        try:
            from PIL import ImageGrab
            self.canvas.update_idletasks()
            x = self.canvas.winfo_rootx()
            y = self.canvas.winfo_rooty()
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            img.save(filepath)
            return True
        except Exception:
            # Fallback to PostScript
            ps_path = filepath.rsplit('.', 1)[0] + '.ps'
            self.canvas.postscript(file=ps_path, colormode='color')
            return False

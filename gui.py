"""
Main GUI window: palette, toolbar, canvas, statusbar, interaction modes.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import json
import math
import os
import sys

from board import Board, PlacedComponent
from components import BUILTIN_COMPONENTS
from component_library import ComponentLibrary
from renderer import BoardRenderer
from persistence import save_board, load_board
from file_lock import is_locked_by_other, write_lock, release_lock


# Interaction modes
MODE_SELECT = 'SELECT'
MODE_PLACE = 'PLACE'
MODE_DELETE = 'DELETE'
MODE_WIRE = 'WIRE'
MODE_DIVIDE = 'DIVIDE'

CLIPBOARD_PREFIX = 'BBSIM:'

WIRE_COLORS = [
    ('#FFFFFF', 'White'),
    ('#FF1744', 'Red'),
    ('#2979FF', 'Blue'),
    ('#00E676', 'Green'),
    ('#FFEA00', 'Yellow'),
    ('#FF9100', 'Orange'),
    ('#000000', 'Black'),
]


# ── Undo/Redo command pattern ──

class Command:
    def execute(self): raise NotImplementedError
    def undo(self): raise NotImplementedError


class PlaceCmd(Command):
    def __init__(self, app, comp_def, row, col, rotation):
        self.app = app
        self.comp_def = comp_def
        self.row = row
        self.col = col
        self.rotation = rotation
        self.comp_id = None

    def execute(self):
        pc = self.app.board.place_component(self.comp_def, self.row, self.col, self.rotation)
        if pc:
            self.comp_id = pc.id
        return pc is not None

    def undo(self):
        if self.comp_id:
            self.app.board.remove_component(self.comp_id)


class DeleteCmd(Command):
    def __init__(self, app, comp_id):
        self.app = app
        self.comp_id = comp_id
        self.pc_data = None  # saved for undo

    def execute(self):
        pc = self.app.board.components.get(self.comp_id)
        if pc:
            self.pc_data = pc.to_dict()
            self.comp_def = pc.comp_def
            self.app.board.remove_component(self.comp_id)
            return True
        return False

    def undo(self):
        if self.pc_data:
            pc = self.app.board.place_component(
                self.comp_def,
                self.pc_data['anchor_row'],
                self.pc_data['anchor_col'],
                self.pc_data['rotation'],
                comp_id=self.pc_data['id'],
            )
            if pc and 'label' in self.pc_data:
                pc.label = self.pc_data['label']


class MoveCmd(Command):
    def __init__(self, app, comp_id, old_row, old_col, new_row, new_col):
        self.app = app
        self.comp_id = comp_id
        self.old_row = old_row
        self.old_col = old_col
        self.new_row = new_row
        self.new_col = new_col

    def execute(self):
        return self.app.board.move_component(self.comp_id, self.new_row, self.new_col)

    def undo(self):
        self.app.board.move_component(self.comp_id, self.old_row, self.old_col)


class RotateCmd(Command):
    def __init__(self, app, comp_id):
        self.app = app
        self.comp_id = comp_id
        self.old_row = None
        self.old_col = None
        self.old_rotation = None

    def execute(self):
        pc = self.app.board.components.get(self.comp_id)
        if not pc:
            return False
        self.old_row = pc.anchor_row
        self.old_col = pc.anchor_col
        self.old_rotation = pc.rotation
        ok, _, _ = self.app.board.rotate_component(self.comp_id)
        return ok

    def undo(self):
        pc = self.app.board.components.get(self.comp_id)
        if pc and self.old_rotation is not None:
            # Clear current cells
            for r, c in pc.get_occupied_cells():
                if self.app.board._in_bounds(r, c):
                    self.app.board.pads[r][c].occupied_by = None
            # Restore old state
            pc.anchor_row = self.old_row
            pc.anchor_col = self.old_col
            pc.rotation = self.old_rotation
            # Mark restored cells
            for r, c in pc.get_occupied_cells():
                if self.app.board._in_bounds(r, c):
                    self.app.board.pads[r][c].occupied_by = self.comp_id


class ReplaceLiftedCmd(Command):
    """Place a lifted (floating) component at a new position/rotation, preserving its id and label."""

    def __init__(self, app, comp_id, comp_def, label, old_row, old_col, old_rot, new_row, new_col, new_rot):
        self.app = app
        self.comp_id = comp_id
        self.comp_def = comp_def
        self.label = label
        self.old_row = old_row
        self.old_col = old_col
        self.old_rot = old_rot
        self.new_row = new_row
        self.new_col = new_col
        self.new_rot = new_rot

    def execute(self):
        placed = self.app.board.place_component(
            self.comp_def, self.new_row, self.new_col, self.new_rot,
            comp_id=self.comp_id,
        )
        if placed:
            if self.label is not None:
                placed.label = self.label
            return True
        return False

    def undo(self):
        self.app.board.remove_component(self.comp_id)
        placed = self.app.board.place_component(
            self.comp_def, self.old_row, self.old_col, self.old_rot,
            comp_id=self.comp_id,
        )
        if placed and self.label is not None:
            placed.label = self.label


class MultiDeleteCmd(Command):
    """Delete multiple components as one undoable action."""

    def __init__(self, app, comp_ids):
        self.app = app
        self.comp_ids = list(comp_ids)
        self.saved = {}  # comp_id -> (comp_def, row, col, rot, label)

    def execute(self):
        for cid in self.comp_ids:
            pc = self.app.board.components.get(cid)
            if pc:
                self.saved[cid] = (pc.comp_def, pc.anchor_row, pc.anchor_col,
                                   pc.rotation, pc.label)
                self.app.board.remove_component(cid)
        return bool(self.saved)

    def undo(self):
        for cid, (comp_def, row, col, rot, label) in self.saved.items():
            placed = self.app.board.place_component(comp_def, row, col, rot, comp_id=cid)
            if placed and label is not None:
                placed.label = label


class MultiMoveCmd(Command):
    """Move multiple components simultaneously as one undoable action."""

    def __init__(self, app, entries):
        # entries: list of (comp_id, comp_def, label, rotation, old_r, old_c, new_r, new_c)
        self.app = app
        self.entries = entries

    def _place_all(self, use_new):
        for e in self.entries:
            self.app.board.remove_component(e[0])
        for comp_id, comp_def, label, rotation, old_r, old_c, new_r, new_c in self.entries:
            r, c = (new_r, new_c) if use_new else (old_r, old_c)
            placed = self.app.board.place_component(comp_def, r, c, rotation, comp_id=comp_id)
            if placed and label is not None:
                placed.label = label

    def execute(self):
        self._place_all(use_new=True)
        return True

    def undo(self):
        self._place_all(use_new=False)


class MultiRotateCmd(Command):
    """Rotate multiple components simultaneously as one undoable action."""

    def __init__(self, app, entries):
        # entries: list of (comp_id, comp_def, label, old_r, old_c, old_rot, new_r, new_c, new_rot)
        self.app = app
        self.entries = entries

    def _place_all(self, use_new):
        for e in self.entries:
            self.app.board.remove_component(e[0])
        for comp_id, comp_def, label, old_r, old_c, old_rot, new_r, new_c, new_rot in self.entries:
            r, c, rot = (new_r, new_c, new_rot) if use_new else (old_r, old_c, old_rot)
            placed = self.app.board.place_component(comp_def, r, c, rot, comp_id=comp_id)
            if placed and label is not None:
                placed.label = label

    def execute(self):
        self._place_all(use_new=True)
        return True

    def undo(self):
        self._place_all(use_new=False)


class AddGuideCmd(Command):
    def __init__(self, app, r1, c1, r2, c2, color='#FFFFFF'):
        self.app = app
        self.r1 = r1
        self.c1 = c1
        self.r2 = r2
        self.c2 = c2
        self.color = color

    def execute(self):
        self.app.board.add_guide(self.r1, self.c1, self.r2, self.c2, self.color)
        return True

    def undo(self):
        if self.app.board.guides:
            self.app.board.guides.pop()


class DeleteGuideCmd(Command):
    """Delete a guide, splitting at junctions so only the clicked segment is removed."""

    def __init__(self, app, index, click_r, click_c):
        self.app = app
        self.index = index
        self.click_r = click_r
        self.click_c = click_c
        self.saved = None
        self.added_count = 0

    def execute(self):
        board = self.app.board
        gl = board.guides[self.index]
        junctions = self._find_junctions(gl, board)

        if len(junctions) <= 2:
            # No intermediate junctions - delete entire guide
            self.saved = board.remove_guide(self.index)
            return self.saved is not None

        # Find which sub-segment the click is in
        best_seg = 0
        best_dist = float('inf')
        for i in range(len(junctions) - 1):
            p1, p2 = junctions[i], junctions[i + 1]
            mr, mc = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            d = math.hypot(self.click_r - mr, self.click_c - mc)
            if d < best_dist:
                best_dist = d
                best_seg = i

        self.saved = board.remove_guide(self.index)
        color = self.saved.color
        self.added_count = 0
        for i in range(len(junctions) - 1):
            if i != best_seg:
                p1, p2 = junctions[i], junctions[i + 1]
                board.add_guide(p1[0], p1[1], p2[0], p2[1], color)
                self.added_count += 1
        return True

    def undo(self):
        board = self.app.board
        for _ in range(self.added_count):
            board.guides.pop()
        if self.saved:
            board.guides.insert(self.index, self.saved)

    @staticmethod
    def _find_junctions(gl, board):
        """Find all points where other guides branch or occupied pads lie on this guide."""
        r1, c1, r2, c2 = gl.r1, gl.c1, gl.r2, gl.c2
        points = {(r1, c1), (r2, c2)}
        # Other guide endpoints on this guide
        for other in board.guides:
            if other is gl:
                continue
            for pr, pc in [(other.r1, other.c1), (other.r2, other.c2)]:
                cross = (pr - r1) * (c2 - c1) - (pc - c1) * (r2 - r1)
                if cross == 0:
                    if (min(r1, r2) <= pr <= max(r1, r2) and
                            min(c1, c2) <= pc <= max(c1, c2)):
                        points.add((pr, pc))
        # Occupied pads along the guide (integer grid points on the line)
        dr, dc = r2 - r1, c2 - c1
        steps = max(abs(dr), abs(dc))
        if steps > 0:
            for s in range(steps + 1):
                pr = r1 + dr * s // steps
                pc = c1 + dc * s // steps
                if (board._in_bounds(pr, pc) and
                        board.pads[pr][pc].occupied_by is not None):
                    points.add((pr, pc))
        # Sort along the segment direction
        return sorted(points, key=lambda p: (p[0] - r1) * dr + (p[1] - c1) * dc)


class AddDivisionCmd(Command):
    def __init__(self, app, r1, c1, r2, c2):
        self.app = app
        self.r1 = r1
        self.c1 = c1
        self.r2 = r2
        self.c2 = c2

    def execute(self):
        self.app.board.add_division(self.r1, self.c1, self.r2, self.c2)
        return True

    def undo(self):
        if self.app.board.divisions:
            self.app.board.divisions.pop()


class DeleteDivisionCmd(Command):
    def __init__(self, app, index):
        self.app = app
        self.index = index
        self.saved = None

    def execute(self):
        self.saved = self.app.board.remove_division(self.index)
        return self.saved is not None

    def undo(self):
        if self.saved:
            self.app.board.divisions.insert(self.index, self.saved)


class MoveDivisionCmd(Command):
    def __init__(self, app, index, old_r1, old_c1, old_r2, old_c2, new_r1, new_c1, new_r2, new_c2):
        self.app = app
        self.index = index
        self.old = (old_r1, old_c1, old_r2, old_c2)
        self.new = (new_r1, new_c1, new_r2, new_c2)

    def execute(self):
        dl = self.app.board.divisions[self.index]
        dl.r1, dl.c1, dl.r2, dl.c2 = self.new
        return True

    def undo(self):
        dl = self.app.board.divisions[self.index]
        dl.r1, dl.c1, dl.r2, dl.c2 = self.old


class RenameCmd(Command):
    def __init__(self, app, comp_id, old_label, new_label,
                 old_size=None, new_size=None,
                 old_align='center', new_align='center'):
        self.app = app
        self.comp_id = comp_id
        self.old_label = old_label
        self.new_label = new_label
        self.old_size = old_size
        self.new_size = new_size
        self.old_align = old_align
        self.new_align = new_align

    def execute(self):
        pc = self.app.board.components.get(self.comp_id)
        if pc:
            pc.label = self.new_label
            pc.label_size = self.new_size
            pc.label_align = self.new_align
            return True
        return False

    def undo(self):
        pc = self.app.board.components.get(self.comp_id)
        if pc:
            pc.label = self.old_label
            pc.label_size = self.old_size
            pc.label_align = self.old_align


# ── Free text label commands ──

class AddTextLabelCmd(Command):
    def __init__(self, app, row, col, props):
        self.app = app
        self.row = row
        self.col = col
        self.props = props
        self.label_id = None

    def execute(self):
        tl = self.app.board.add_text_label(self.row, self.col, self.props.get('text', ''))
        tl.size = self.props.get('size')
        tl.align = self.props.get('align', 'center')
        tl.opacity = self.props.get('opacity', 100)
        tl.layer = self.props.get('layer', 'above')
        tl.color = self.props.get('color', '#E0E0E0')
        tl.bg_color = self.props.get('bg_color', '#000000')
        tl.border_color = self.props.get('border_color', '')
        self.label_id = tl.id
        return True

    def undo(self):
        if self.label_id:
            self.app.board.remove_text_label(self.label_id)


class DeleteTextLabelCmd(Command):
    def __init__(self, app, label_id):
        self.app = app
        self.label_id = label_id
        self._saved = None

    def execute(self):
        tl = self.app.board.get_text_label(self.label_id)
        if tl:
            self._saved = tl.to_dict()
            self.app.board.remove_text_label(self.label_id)
            return True
        return False

    def undo(self):
        if self._saved:
            from board import TextLabel
            tl = TextLabel.from_dict(self._saved)
            self.app.board.text_labels.append(tl)


class MoveTextLabelCmd(Command):
    def __init__(self, app, label_id, old_row, old_col, new_row, new_col):
        self.app = app
        self.label_id = label_id
        self.old_row, self.old_col = old_row, old_col
        self.new_row, self.new_col = new_row, new_col

    def execute(self):
        tl = self.app.board.get_text_label(self.label_id)
        if tl:
            tl.row, tl.col = self.new_row, self.new_col
            return True
        return False

    def undo(self):
        tl = self.app.board.get_text_label(self.label_id)
        if tl:
            tl.row, tl.col = self.old_row, self.old_col


class EditTextLabelCmd(Command):
    def __init__(self, app, label_id, old_props, new_props):
        self.app = app
        self.label_id = label_id
        self.old_props = old_props
        self.new_props = new_props

    def _apply(self, props):
        tl = self.app.board.get_text_label(self.label_id)
        if tl:
            tl.text = props['text']
            tl.size = props.get('size')
            tl.align = props.get('align', 'center')
            tl.opacity = props.get('opacity', 100)
            tl.layer = props.get('layer', 'above')
            tl.color = props.get('color', '#E0E0E0')
            tl.bg_color = props.get('bg_color', '#000000')
            tl.border_color = props.get('border_color', '')

    def execute(self):
        self._apply(self.new_props)
        return True

    def undo(self):
        self._apply(self.old_props)


class RotateTextLabelCmd(Command):
    def __init__(self, app, label_id, old_rot, new_rot):
        self.app = app
        self.label_id = label_id
        self.old_rot = old_rot
        self.new_rot = new_rot

    def execute(self):
        tl = self.app.board.get_text_label(self.label_id)
        if tl:
            tl.rotation = self.new_rot
            return True
        return False

    def undo(self):
        tl = self.app.board.get_text_label(self.label_id)
        if tl:
            tl.rotation = self.old_rot


# ── Label edit dialog ──

_LABEL_FONT_SIZES = ['auto', '6', '7', '8', '9', '10', '11', '12', '14',
                     '16', '18', '20', '24', '28', '32', '36', '48', '60', '72']


class LabelEditDialog(tk.Toplevel):
    """Custom dialog for editing a component label (multi-line), font size and alignment."""

    def __init__(self, parent, title, prompt, initial_text='',
                 initial_size=None, initial_align='center', select_all=True):
        super().__init__(parent)
        self.result_text = None   # None = cancelled
        self.result_size = None
        self.result_align = 'center'

        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Prompt label
        tk.Label(self, text=prompt, anchor='w', justify='left').pack(
            fill='x', padx=10, pady=(10, 2))

        # Multi-line text area
        txt_frame = tk.Frame(self)
        txt_frame.pack(fill='both', expand=True, padx=10)
        self._text = tk.Text(txt_frame, width=32, height=5, wrap='word',
                             font=('Consolas', 10), relief='sunken', bd=1)
        sb = tk.Scrollbar(txt_frame, command=self._text.yview)
        self._text.config(yscrollcommand=sb.set)
        self._text.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        if initial_text:
            self._text.insert('1.0', initial_text)
        if select_all:
            self._text.tag_add('sel', '1.0', 'end')
        else:
            self._text.mark_set('insert', 'end')
        self._text.focus_set()

        # Hint
        tk.Label(self, text='Enter = confirm    Ctrl+Enter = new line',
                 fg='#888888', font=('TkDefaultFont', 8)).pack(
            anchor='w', padx=10)

        # Font size + alignment row
        opts_frame = tk.Frame(self)
        opts_frame.pack(fill='x', padx=10, pady=6)

        tk.Label(opts_frame, text='Size:').pack(side='left')
        self._size_var = tk.StringVar()
        size_cb = ttk.Combobox(opts_frame, textvariable=self._size_var,
                               values=_LABEL_FONT_SIZES, width=7, state='readonly')
        size_cb.pack(side='left', padx=(4, 14))
        self._size_var.set('auto' if initial_size is None else str(initial_size))

        tk.Label(opts_frame, text='Align:').pack(side='left')
        self._align_var = tk.StringVar(value=initial_align)
        for val, symbol in (('left', '◀ Left'), ('center', '● Center'), ('right', 'Right ▶')):
            tk.Radiobutton(opts_frame, text=symbol, variable=self._align_var,
                           value=val).pack(side='left', padx=2)

        # OK / Cancel buttons
        btn_frame = tk.Frame(self)
        btn_frame.pack(fill='x', padx=10, pady=(0, 10))
        tk.Button(btn_frame, text='OK', command=self._ok, width=9).pack(side='right', padx=2)
        tk.Button(btn_frame, text='Cancel', command=self.destroy, width=9).pack(side='right', padx=2)

        self.bind('<Escape>', lambda e: self.destroy())
        self._text.bind('<Return>', lambda e: (self._ok(), 'break')[1])
        self._text.bind('<Control-Return>', lambda e: self._text.insert('insert', '\n') or 'break')

        # Centre over parent
        self.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f'+{px + (pw - w) // 2}+{py + (ph - h) // 2}')

        self.wait_window(self)

    def _ok(self):
        self.result_text = self._text.get('1.0', 'end-1c')
        sv = self._size_var.get()
        self.result_size = None if sv == 'auto' else int(sv)
        self.result_align = self._align_var.get()
        self.destroy()


# ── Free text label dialog ──

_SWATCH_COLORS = [
    ('#E0E0E0', 'Light Gray'), ('#FFFFFF', 'White'),
    ('#FFFF00', 'Yellow'),     ('#FF9900', 'Orange'),
    ('#FF4444', 'Red'),        ('#44FF44', 'Green'),
    ('#44AAFF', 'Blue'),       ('#000000', 'Black'),
    ('#4444CC', 'Dark Blue'),  ('#228B22', 'Dark Green'),
    ('#CC3333', 'Dark Red'),   ('#888888', 'Gray'),
    ('#CC8800', 'Amber'),      ('#884488', 'Purple'),
    ('#008888', 'Teal'),
]


class TextLabelDialog(tk.Toplevel):
    """Dialog to create / edit a free-floating board text label."""

    def __init__(self, parent, title='Text Label', initial=None):
        super().__init__(parent)
        if initial is None:
            initial = {}
        self.result = None   # None = cancelled, else dict

        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Text area
        tk.Label(self, text='Text:', anchor='w').pack(fill='x', padx=10, pady=(10, 2))
        txt_frame = tk.Frame(self)
        txt_frame.pack(fill='both', expand=True, padx=10)
        self._text = tk.Text(txt_frame, width=34, height=5, wrap='word',
                             font=('Consolas', 10), relief='sunken', bd=1)
        sb = tk.Scrollbar(txt_frame, command=self._text.yview)
        self._text.config(yscrollcommand=sb.set)
        self._text.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        has_text = bool(initial.get('text'))
        if has_text:
            self._text.insert('1.0', initial['text'])
        if not has_text:
            self._text.tag_add('sel', '1.0', 'end')
        else:
            self._text.mark_set('insert', 'end')
        self._text.focus_set()
        tk.Label(self, text='Enter = confirm    Ctrl+Enter = new line',
                 fg='#888888', font=('TkDefaultFont', 8)).pack(anchor='w', padx=10)

        # Row 1: size + alignment
        r1 = tk.Frame(self)
        r1.pack(fill='x', padx=10, pady=(6, 2))
        tk.Label(r1, text='Size:').pack(side='left')
        self._size_var = tk.StringVar()
        ttk.Combobox(r1, textvariable=self._size_var, values=_LABEL_FONT_SIZES,
                     width=7, state='readonly').pack(side='left', padx=(4, 14))
        self._size_var.set('auto' if initial.get('size') is None else str(initial['size']))
        tk.Label(r1, text='Align:').pack(side='left')
        self._align_var = tk.StringVar(value=initial.get('align', 'center'))
        for val, sym in (('left', '◀ Left'), ('center', '● Ctr'), ('right', 'Right ▶')):
            tk.Radiobutton(r1, text=sym, variable=self._align_var,
                           value=val).pack(side='left', padx=2)

        # Row 2: background opacity + layer
        r2 = tk.Frame(self)
        r2.pack(fill='x', padx=10, pady=(2, 2))
        tk.Label(r2, text='BG opacity:').pack(side='left')
        self._opacity_var = tk.IntVar(value=initial.get('opacity', 100))
        tk.Scale(r2, from_=0, to=100, orient='horizontal', variable=self._opacity_var,
                 length=110, showvalue=True).pack(side='left', padx=(4, 14))
        tk.Label(r2, text='Layer:').pack(side='left')
        self._layer_var = tk.StringVar(value=initial.get('layer', 'above'))
        tk.Radiobutton(r2, text='Above comps', variable=self._layer_var,
                       value='above').pack(side='left', padx=2)
        tk.Radiobutton(r2, text='Below comps', variable=self._layer_var,
                       value='below').pack(side='left', padx=2)

        # Three color rows share a grid so column 0 auto-sizes to the widest label
        # and all swatch columns start at the same x position.
        cgrid = tk.Frame(self)
        cgrid.pack(fill='x', padx=10, pady=(2, 8))

        def _swatches_frame(parent, grid_row):
            f = tk.Frame(parent)
            f.grid(row=grid_row, column=1, sticky='w', pady=2)
            return f

        # Row 0: Text color
        tk.Label(cgrid, text='Text color:', anchor='w').grid(
            row=0, column=0, sticky='w', padx=(0, 6), pady=2)
        r3 = _swatches_frame(cgrid, 0)
        self._color_var = tk.StringVar(value=initial.get('color', '#E0E0E0'))
        self._color_btns = {}
        for hex_c, _ in _SWATCH_COLORS:
            btn = tk.Button(r3, width=2, height=1, bg=hex_c, activebackground=hex_c,
                            relief='sunken' if hex_c == self._color_var.get() else 'raised',
                            bd=2, command=lambda c=hex_c: self._pick_color('color', c))
            btn.pack(side='left', padx=1)
            self._color_btns[hex_c] = btn

        # Row 1: BG color
        tk.Label(cgrid, text='BG color:', anchor='w').grid(
            row=1, column=0, sticky='w', padx=(0, 6), pady=2)
        r4 = _swatches_frame(cgrid, 1)
        self._bg_color_var = tk.StringVar(value=initial.get('bg_color', '#000000'))
        self._bg_color_btns = {}
        for hex_c, _ in _SWATCH_COLORS:
            btn = tk.Button(r4, width=2, height=1, bg=hex_c, activebackground=hex_c,
                            relief='sunken' if hex_c == self._bg_color_var.get() else 'raised',
                            bd=2, command=lambda c=hex_c: self._pick_color('bg', c))
            btn.pack(side='left', padx=1)
            self._bg_color_btns[hex_c] = btn

        # Row 2: Border — label + ╳ share column 0, swatches in column 1
        border_prefix = tk.Frame(cgrid)
        border_prefix.grid(row=2, column=0, sticky='w', pady=2)
        tk.Label(border_prefix, text='Border:', anchor='w').pack(side='left')
        self._border_color_var = tk.StringVar(value=initial.get('border_color', ''))
        self._border_color_btns = {}
        no_btn = tk.Button(border_prefix, text='╳', width=2, height=1,
                           relief='sunken' if self._border_color_var.get() == '' else 'raised',
                           bd=2, command=lambda: self._pick_color('border', ''))
        no_btn.pack(side='left', padx=(6, 0))
        self._border_color_btns[''] = no_btn
        r5 = _swatches_frame(cgrid, 2)
        for hex_c, _ in _SWATCH_COLORS:
            btn = tk.Button(r5, width=2, height=1, bg=hex_c, activebackground=hex_c,
                            relief='sunken' if hex_c == self._border_color_var.get() else 'raised',
                            bd=2, command=lambda c=hex_c: self._pick_color('border', c))
            btn.pack(side='left', padx=1)
            self._border_color_btns[hex_c] = btn

        # Buttons
        bf = tk.Frame(self)
        bf.pack(fill='x', padx=10, pady=(0, 10))
        tk.Button(bf, text='OK', command=self._ok, width=9).pack(side='right', padx=2)
        tk.Button(bf, text='Cancel', command=self.destroy, width=9).pack(side='right', padx=2)

        self.bind('<Escape>', lambda e: self.destroy())
        self._text.bind('<Return>', lambda e: (self._ok(), 'break')[1])
        self._text.bind('<Control-Return>', lambda e: self._text.insert('insert', '\n') or 'break')

        self.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f'+{px + (pw - w) // 2}+{py + (ph - h) // 2}')
        self.wait_window(self)

    def _pick_color(self, which, color):
        if which == 'color':
            for c, btn in self._color_btns.items():
                btn.config(relief='sunken' if c == color else 'raised')
            self._color_var.set(color)
        elif which == 'bg':
            for c, btn in self._bg_color_btns.items():
                btn.config(relief='sunken' if c == color else 'raised')
            self._bg_color_var.set(color)
        elif which == 'border':
            for c, btn in self._border_color_btns.items():
                btn.config(relief='sunken' if c == color else 'raised')
            self._border_color_var.set(color)

    def _ok(self):
        sv = self._size_var.get()
        self.result = {
            'text':         self._text.get('1.0', 'end-1c'),
            'size':         None if sv == 'auto' else int(sv),
            'align':        self._align_var.get(),
            'opacity':      self._opacity_var.get(),
            'layer':        self._layer_var.get(),
            'color':        self._color_var.get(),
            'bg_color':     self._bg_color_var.get(),
            'border_color': self._border_color_var.get(),
        }
        self.destroy()


# ── Board size presets ──

BOARD_PRESETS = {
    '50x70mm (20x28)': (20, 28),
    '70x90mm (28x36)': (28, 36),
    'SBC-WALL (74x57)': (74, 57),
}


class BreadboardApp:
    """Main application class."""

    def __init__(self, root):
        self.root = root
        self.root.title("Breadboard Simulator")
        self.root.geometry("1200x800")

        self.library = ComponentLibrary()
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(__file__))
        lib_dir = os.path.join(base_dir, 'library')
        self.library.load_json_dir(lib_dir)

        self.board = Board(74, 57)  # SBC-WALL default (rotated/portrait)
        self.filepath = None
        self.modified = False
        # Use APPDATA (stable across PyInstaller runs) or home dir as fallback
        appdata = os.environ.get('APPDATA', os.path.expanduser('~'))
        self._recent_path = os.path.join(appdata, '.bbsim-recent')
        self._geometry_path = os.path.join(appdata, '.bbsim-geometry')

        self.mode = MODE_SELECT
        self.place_comp_def = None
        self.place_rotation = 0
        self.selected_comp_ids = set()   # set of currently selected component IDs
        self._lifted_comp_id = None    # id of component lifted for re-placement
        self._lifted_comp_def = None
        self._lifted_comp_label = None
        self._lifted_orig_row = None
        self._lifted_orig_col = None
        self._lifted_orig_rot = None
        self._drag_start = None
        self._drag_comp_id = None
        self._drag_orig_row = None
        self._drag_orig_col = None
        self._drag_orig_positions = {}    # {comp_id: (orig_row, orig_col)} for multi-drag
        self._drag_orig_center_row = None  # visual center of anchor comp (or group centroid)
        self._drag_orig_center_col = None
        self._drag_started_moving = False  # True once cursor crossed 5-px threshold
        self._pending_single_select = None  # comp_id to collapse to on click-without-drag
        self._rubberband_start = None     # (canvas_x, canvas_y) rubber-band origin
        # Multi-ghost state (used when rotating a group with collisions)
        self._multi_ghost_entries = []    # list of dicts with comp ghost state
        self._multi_ghost_ref_row = None  # group centroid row at ghost-start
        self._multi_ghost_ref_col = None  # group centroid col at ghost-start
        self._paste_label = None     # label to apply after paste-place
        self._wire_color = '#FFFFFF'  # currently selected wire color
        self._wire_start = None      # (row, col) of wire start pad
        self._division_start = None  # (row, col) of division start edge point
        self._drag_div_idx = -1      # index of division being dragged
        self._drag_div_orig = None   # (r1, c1, r2, c2) before drag
        self._file_lock_path = None  # filepath currently holding our lock
        # Free text label interaction
        self._selected_text_label_id = None
        self._drag_text_label_id = None
        self._ghost_text_label_props = None  # props dict while positioning a new label
        self._drag_text_label_orig = None   # (row, col) at drag start
        self._drag_text_label_click = None  # (float_row, float_col) of cursor at click
        self._drag_text_label_started = False

        # Undo/redo
        self._undo_stack = []
        self._redo_stack = []

        self._build_ui()
        self._bind_keys()
        self.root.protocol('WM_DELETE_WINDOW', self._quit)
        self._restore_geometry()
        self.root.after(100, self._startup_load)

    def _build_ui(self):
        # Menu bar
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New...", command=self._new_board, accelerator="Ctrl+N")
        file_menu.add_command(label="Open...", command=self._open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Save", command=self._save_file, accelerator="Ctrl+S")
        file_menu.add_command(label="Save As...", command=self._save_as)
        file_menu.add_separator()
        file_menu.add_command(label="Export PNG...", command=self._export_png)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._quit, accelerator="Ctrl+Q")

        # Edit menu
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo", command=self._undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=self._redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", command=self._cut_selected, accelerator="Ctrl+X")
        edit_menu.add_command(label="Copy", command=self._copy_selected, accelerator="Ctrl+C")
        edit_menu.add_command(label="Paste", command=self._paste_clipboard, accelerator="Ctrl+V")
        edit_menu.add_separator()
        edit_menu.add_command(label="Delete Selected", command=self._delete_selected, accelerator="Del")

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Zoom In", command=lambda: self.renderer.zoom_in(), accelerator="+")
        view_menu.add_command(label="Zoom Out", command=lambda: self.renderer.zoom_out(), accelerator="-")
        view_menu.add_command(label="Zoom Fit", command=self.renderer.zoom_fit if hasattr(self, 'renderer') else None, accelerator="F")
        view_menu.add_separator()
        self._show_grid_var = tk.BooleanVar(value=True)
        view_menu.add_checkbutton(label="Show Grid", variable=self._show_grid_var, command=self._toggle_grid)
        self._show_labels_var = tk.BooleanVar(value=True)
        view_menu.add_checkbutton(label="Show Labels", variable=self._show_labels_var, command=self._toggle_labels)
        view_menu.add_separator()
        # Row label config
        row_label_menu = tk.Menu(view_menu, tearoff=0)
        view_menu.add_cascade(label="Row Labels", menu=row_label_menu)
        self._row_mode_var = tk.StringVar(value='num')
        row_label_menu.add_radiobutton(label="Numbers (1,2,3...)", variable=self._row_mode_var,
                                       value='num', command=self._update_label_config)
        row_label_menu.add_radiobutton(label="Letters (A,B,C...)", variable=self._row_mode_var,
                                       value='alpha', command=self._update_label_config)
        row_label_menu.add_separator()
        self._row_dir_var = tk.StringVar(value='asc')
        row_label_menu.add_radiobutton(label="Top \u2192 Bottom", variable=self._row_dir_var,
                                       value='asc', command=self._update_label_config)
        row_label_menu.add_radiobutton(label="Bottom \u2192 Top", variable=self._row_dir_var,
                                       value='desc', command=self._update_label_config)
        # Col label config
        col_label_menu = tk.Menu(view_menu, tearoff=0)
        view_menu.add_cascade(label="Column Labels", menu=col_label_menu)
        self._col_mode_var = tk.StringVar(value='num')
        col_label_menu.add_radiobutton(label="Numbers (1,2,3...)", variable=self._col_mode_var,
                                       value='num', command=self._update_label_config)
        col_label_menu.add_radiobutton(label="Letters (A,B,C...)", variable=self._col_mode_var,
                                       value='alpha', command=self._update_label_config)
        col_label_menu.add_separator()
        self._col_dir_var = tk.StringVar(value='asc')
        col_label_menu.add_radiobutton(label="Left \u2192 Right", variable=self._col_dir_var,
                                       value='asc', command=self._update_label_config)
        col_label_menu.add_radiobutton(label="Right \u2192 Left", variable=self._col_dir_var,
                                       value='desc', command=self._update_label_config)

        # Board menu
        board_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Board", menu=board_menu)
        board_menu.add_command(label="Rotate Board 90\u00b0 CW", command=self._rotate_board, accelerator="Ctrl+R")
        board_menu.add_separator()
        board_menu.add_command(label="Resize...", command=self._resize_board)
        board_menu.add_command(label="Clear All", command=self._clear_board)
        board_menu.add_command(label="Info", command=self._board_info)

        # ── Layout: palette | canvas ──
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Left palette
        palette_frame = ttk.Frame(paned, width=220)
        paned.add(palette_frame, weight=0)

        # Toolbar in palette
        tb = ttk.Frame(palette_frame)
        tb.pack(fill=tk.X, padx=2, pady=2)

        self._mode_var = tk.StringVar(value=MODE_SELECT)
        modes = [(MODE_SELECT, "Select"), (MODE_PLACE, "Place"),
                 (MODE_WIRE, "Wire"), (MODE_DIVIDE, "Divide"), (MODE_DELETE, "Delete")]
        for val, text in modes:
            ttk.Radiobutton(tb, text=text, variable=self._mode_var,
                            value=val, command=self._mode_changed).pack(side=tk.LEFT, padx=2)

        tb2 = ttk.Frame(palette_frame)
        tb2.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(tb2, text="100%", width=5,
                   command=lambda: self.renderer.zoom_fit()).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb2, text="+", width=3,
                   command=lambda: self.renderer.zoom_in()).pack(side=tk.LEFT, padx=1)
        ttk.Button(tb2, text="\u2013", width=3,
                   command=lambda: self.renderer.zoom_out()).pack(side=tk.LEFT, padx=1)
        ttk.Separator(tb2, orient='vertical').pack(side=tk.LEFT, padx=4, fill='y')
        ttk.Button(tb2, text="T+", width=4,
                   command=self._add_text_label).pack(side=tk.LEFT, padx=1)

        # Wire color palette
        color_frame = ttk.Frame(palette_frame)
        color_frame.pack(fill=tk.X, padx=2, pady=(4, 0))
        ttk.Label(color_frame, text="Wire:", font=('', 8)).pack(side=tk.LEFT, padx=(2, 4))
        self._color_buttons = {}
        for hex_color, name in WIRE_COLORS:
            btn = tk.Button(color_frame, width=2, height=1, bg=hex_color,
                            activebackground=hex_color, relief='raised', bd=1,
                            command=lambda c=hex_color, n=name: self._set_wire_color(c, n))
            btn.pack(side=tk.LEFT, padx=1)
            self._color_buttons[hex_color] = btn
        # Highlight default selection
        self._color_buttons['#FFFFFF'].config(relief='sunken', bd=2)

        ttk.Label(palette_frame, text="Components:", font=('', 9, 'bold')).pack(anchor='w', padx=4, pady=(6, 2))

        # Treeview palette
        tree_frame = ttk.Frame(palette_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=2)

        self.tree = ttk.Treeview(tree_frame, show='tree', selectmode='browse')
        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._populate_palette()
        self.tree.bind('<<TreeviewSelect>>', self._on_palette_select)

        # Right canvas area
        canvas_frame = ttk.Frame(paned)
        paned.add(canvas_frame, weight=1)

        self.canvas = tk.Canvas(canvas_frame, bg='#263238', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.configure(takefocus=True)

        self.renderer = BoardRenderer(self.canvas, self.board)

        # Fix zoom fit reference
        view_menu.entryconfigure(2, command=self.renderer.zoom_fit)

        # Statusbar: left (context) + right (coordinates)
        status_frame = ttk.Frame(self.root, relief=tk.SUNKEN, borderwidth=1)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.statusbar = ttk.Label(status_frame, text="Ready\n", anchor='w')
        self.statusbar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._coord_bar = ttk.Label(status_frame, text="", anchor='e')
        self._coord_bar.pack(side=tk.RIGHT)

        # Canvas bindings
        self.canvas.bind('<Button-1>', self._on_click)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Button-2>', self.renderer.start_pan)
        self.canvas.bind('<B2-Motion>', self.renderer.do_pan)
        self.canvas.bind('<Double-Button-1>', self._on_double_click)
        self.canvas.bind('<Button-3>', lambda e: self._cancel())
        self.canvas.bind('<Motion>', self._on_mouse_move)
        self.canvas.bind('<MouseWheel>', self._on_mousewheel)
        self.canvas.bind('<Configure>', lambda e: self.renderer.redraw())
        self.canvas.bind('<Enter>', lambda e: self.canvas.focus_set())

    def _populate_palette(self):
        self.tree.delete(*self.tree.get_children())
        self._tree_map = {}  # tree item id -> ComponentDef
        for cat_name, comps in self.library.get_categories().items():
            cat_id = self.tree.insert('', 'end', text=cat_name, open=True)
            for comp in comps:
                pin_info = f"({len(comp.pins)} pins)"
                item_id = self.tree.insert(cat_id, 'end', text=f"{comp.name} {pin_info}")
                self._tree_map[item_id] = comp

    def _bind_keys(self):
        self.root.bind('<Control-z>', lambda e: self._undo())
        self.root.bind('<Control-Z>', lambda e: self._undo())
        self.root.bind('<Control-y>', lambda e: self._redo())
        self.root.bind('<Control-Y>', lambda e: self._redo())
        self.root.bind('<Control-s>', lambda e: self._save_file())
        self.root.bind('<Control-S>', lambda e: self._save_file())
        self.root.bind('<Control-o>', lambda e: self._open_file())
        self.root.bind('<Control-O>', lambda e: self._open_file())
        self.root.bind('<Control-n>', lambda e: self._new_board())
        self.root.bind('<Control-N>', lambda e: self._new_board())
        self.root.bind('<Control-q>', lambda e: self._quit())
        self.root.bind('<Control-Q>', lambda e: self._quit())
        self.root.bind('<Control-x>', lambda e: self._cut_selected())
        self.root.bind('<Control-X>', lambda e: self._cut_selected())
        self.root.bind('<Control-c>', lambda e: self._copy_selected())
        self.root.bind('<Control-C>', lambda e: self._copy_selected())
        self.root.bind('<Control-v>', lambda e: self._paste_clipboard())
        self.root.bind('<Control-V>', lambda e: self._paste_clipboard())
        self.root.bind('<Control-r>', lambda e: self._rotate_board())
        self.root.bind('<Control-R>', lambda e: self._rotate_board())
        self.root.bind('<Delete>', lambda e: self._delete_selected())
        self.root.bind('<r>', lambda e: self._rotate())
        self.root.bind('<R>', lambda e: self._rotate())
        self.root.bind('<Escape>', lambda e: self._cancel())
        self.root.bind('<f>', lambda e: self.renderer.zoom_fit())
        self.root.bind('<F>', lambda e: self.renderer.zoom_fit())
        self.root.bind('<plus>', lambda e: self.renderer.zoom_in())
        self.root.bind('<equal>', lambda e: self.renderer.zoom_in())
        self.root.bind('<minus>', lambda e: self.renderer.zoom_out())
        self.root.bind('<w>', lambda e: self._set_mode(MODE_WIRE))
        self.root.bind('<W>', lambda e: self._set_mode(MODE_WIRE))
        self.root.bind('<d>', lambda e: self._set_mode(MODE_DIVIDE))
        self.root.bind('<D>', lambda e: self._set_mode(MODE_DIVIDE))
        self.root.bind('<x>', lambda e: self._set_mode(MODE_DELETE))
        self.root.bind('<X>', lambda e: self._set_mode(MODE_DELETE))
        self.root.bind('<Up>', lambda e: self._move_selected(- 1, 0))
        self.root.bind('<Down>', lambda e: self._move_selected(1, 0))
        self.root.bind('<Left>', lambda e: self._move_selected(0, -1))
        self.root.bind('<Right>', lambda e: self._move_selected(0, 1))
        self.root.bind('<Control-a>', lambda e: self._select_all())
        self.root.bind('<Control-A>', lambda e: self._select_all())

    # ── Mode handling ──

    def _set_mode(self, mode):
        """Switch to a mode programmatically (from keyboard shortcut)."""
        self._mode_var.set(mode)
        self._mode_changed()

    def _mode_changed(self):
        new_mode = self._mode_var.get()
        if self.mode == MODE_PLACE and new_mode != MODE_PLACE:
            if self._multi_ghost_entries:
                self._restore_multi_ghost()
            elif self._lifted_comp_id:
                self._restore_lifted()
        self.mode = new_mode
        if self.mode != MODE_PLACE:
            self.renderer.ghost = None
            self.renderer.multi_ghost = None
            self._paste_label = None
        if self.mode != MODE_WIRE:
            self._wire_start = None
            self.renderer.guide_preview = None
        if self.mode != MODE_DIVIDE:
            self._division_start = None
            self.renderer.division_preview = None
        if self.mode != MODE_SELECT:
            self._clear_selection()
            self.renderer.selected_division = -1
            self._drag_div_idx = -1
        cursors = {MODE_DELETE: 'X_cursor', MODE_WIRE: 'pencil', MODE_DIVIDE: 'tcross'}
        self.canvas.config(cursor=cursors.get(self.mode, ''))
        self.renderer.redraw()
        self._update_status()

    def _set_wire_color(self, hex_color, name):
        self._wire_color = hex_color
        for c, btn in self._color_buttons.items():
            btn.config(relief='sunken' if c == hex_color else 'raised',
                       bd=2 if c == hex_color else 1)
        self._update_status(f"Wire color: {name}")

    def _cancel(self):
        """Escape / right-click: cancel current operation or exit mode."""
        if self._ghost_text_label_props is not None:
            self._ghost_text_label_props = None
            self.renderer.text_label_ghost = None
            self.renderer.redraw()
            self._update_status()
            return
        if self.mode == MODE_WIRE and self._wire_start:
            self._wire_start = None
            self.renderer.guide_preview = None
            self.renderer.redraw()
            self._update_status("Wire: click start pad")
            return
        if self.mode == MODE_DIVIDE and self._division_start:
            self._division_start = None
            self.renderer.division_preview = None
            self.renderer.redraw()
            self._update_status("Divide: click start edge")
            return
        if self.mode in (MODE_DELETE, MODE_WIRE, MODE_DIVIDE):
            self.mode = MODE_SELECT
            self._mode_var.set(MODE_SELECT)
            self._wire_start = None
            self.renderer.guide_preview = None
            self._division_start = None
            self.renderer.division_preview = None
            self.canvas.config(cursor='')
            self.renderer.redraw()
            self._update_status()
            return
        if self.mode == MODE_PLACE:
            if self._multi_ghost_entries:
                self._restore_multi_ghost()
            elif self._lifted_comp_id:
                self._restore_lifted()
            self.mode = MODE_SELECT
            self._mode_var.set(MODE_SELECT)
            self.renderer.ghost = None
            self.renderer.multi_ghost = None
            self.renderer.redraw()
        # In SELECT mode, Esc clears selection
        self._clear_selection()
        self.renderer.redraw()
        self._update_status()

    # ── Palette ──

    def _on_palette_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        comp = self._tree_map.get(sel[0])
        if comp:
            self.place_comp_def = comp
            self.place_rotation = 0
            self._paste_label = None
            self._set_mode(MODE_PLACE)
            self._update_status(f"Place: {comp.name} (R to rotate)")
            self.canvas.focus_set()

    # ── Canvas interaction ──

    def _clamp_to_edges(self, row, col):
        """Clamp to valid grid-edge range [0, rows] x [0, cols]."""
        return (max(0, min(row, self.board.rows)),
                max(0, min(col, self.board.cols)))

    def _snap_division_hv(self, sr, sc, er, ec):
        """Snap end point so division is strictly horizontal or vertical."""
        dr = abs(er - sr)
        dc = abs(ec - sc)
        if dc >= dr:
            return (sr, ec)  # horizontal: same row
        else:
            return (er, sc)  # vertical: same col

    def _on_double_click(self, event):
        """Double-click: edit text label, rename component, or create text label on empty space."""
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        # 'above' labels take priority over components; 'below' labels only if no component
        tl = self._get_text_label_at(event.x, event.y, layer='above')
        pc = self.board.get_component_at(row, col) if tl is None else None
        if tl is None and pc is None:
            tl = self._get_text_label_at(event.x, event.y, layer='below')
        if tl:
            self._edit_text_label(tl)
            return

        if pc is None:
            # Double-click on empty board → create new text label
            self._rubberband_start = None
            self.renderer.selection_rect = None
            self._add_text_label(event.x, event.y)
            return
        # Only rename when this component is the sole selection
        if len(self.selected_comp_ids) > 1:
            return
        dlg = LabelEditDialog(
            self.root,
            title="Edit Label",
            prompt=f"{pc.id}  ({pc.comp_def.name})",
            initial_text=pc.label or pc.id,
            initial_size=pc.label_size,
            initial_align=pc.label_align,
            select_all=pc.label is None,
        )
        if dlg.result_text is not None:
            new_label = dlg.result_text.strip() or None
            new_size = dlg.result_size
            new_align = dlg.result_align
            old_label, old_size, old_align = pc.label, pc.label_size, pc.label_align
            if new_label != old_label or new_size != old_size or new_align != old_align:
                cmd = RenameCmd(self, pc.id, old_label, new_label,
                                old_size, new_size, old_align, new_align)
                self._execute_cmd(cmd)
                self.renderer.redraw()

    def _on_click(self, event):
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        row, col = self._clamp_to_board(row, col)

        if self.mode == MODE_DIVIDE:
            er, ec = self.renderer.canvas_to_edge(event.x, event.y)
            er, ec = self._clamp_to_edges(er, ec)
            if self._division_start is None:
                self._division_start = (er, ec)
                self._update_status(f"Divide: from edge ({er},{ec}) - click end (Esc to cancel)")
            else:
                sr, sc = self._division_start
                er, ec = self._snap_division_hv(sr, sc, er, ec)
                if (sr, sc) != (er, ec):
                    cmd = AddDivisionCmd(self, sr, sc, er, ec)
                    self._execute_cmd(cmd)
                    self._update_status(f"Division: ({sr},{sc}) -> ({er},{ec})")
                self._division_start = None
                self.renderer.division_preview = None
                self.renderer.redraw()
            return

        elif self.mode == MODE_PLACE and self._multi_ghost_entries:
            # Multi-ghost placement: place all components at offset positions
            dr = row - self._multi_ghost_ref_row
            dc = col - self._multi_ghost_ref_col
            entries = []
            for e in self._multi_ghost_entries:
                gr = self._clamp_to_board(e['base_row'] + dr, e['base_col'] + dc)[0]
                gc = self._clamp_to_board(e['base_row'] + dr, e['base_col'] + dc)[1]
                entries.append((e['comp_id'], e['comp_def'], e['label'],
                                e['old_row'], e['old_col'], e['old_rot'],
                                gr, gc, e['new_rot']))
            all_valid = all(
                self.board.can_place(comp_def, gr, gc, new_rot)
                for _, comp_def, _, _, _, _, gr, gc, new_rot in entries
            )
            if all_valid:
                cmd = MultiRotateCmd(self, entries)
                self._execute_cmd(cmd)
                self._clear_multi_ghost()
                self._set_selection({e[0] for e in entries})
                self._set_mode(MODE_SELECT)
                self._update_status(f"Rotated {len(entries)} component(s)")
            else:
                self._update_status("Cannot place here — collision or out of bounds")
            self.renderer.redraw()

        elif self.mode == MODE_PLACE and self.place_comp_def:
            # Use ghost's pre-computed center-adjusted anchor instead of raw cursor position
            if self.renderer.ghost:
                _, place_row, place_col, _, _ = self.renderer.ghost
            else:
                place_row, place_col = row, col
            if self._lifted_comp_id:
                # Re-placing a lifted component — preserve its id and label
                cmd = ReplaceLiftedCmd(
                    self,
                    self._lifted_comp_id, self._lifted_comp_def, self._lifted_comp_label,
                    self._lifted_orig_row, self._lifted_orig_col, self._lifted_orig_rot,
                    place_row, place_col, self.place_rotation,
                )
                if self._execute_cmd(cmd):
                    placed_id = self._lifted_comp_id
                    self._clear_lifted()
                    self.mode = MODE_SELECT
                    self._mode_var.set(MODE_SELECT)
                    self.renderer.ghost = None
                    self._set_selection({placed_id})
                    self._update_status(f"Placed {placed_id}")
                else:
                    self._update_status("Cannot place here - collision or out of bounds")
            else:
                cmd = PlaceCmd(self, self.place_comp_def, place_row, place_col, self.place_rotation)
                if self._execute_cmd(cmd):
                    # Apply pasted label if present
                    if self._paste_label and cmd.comp_id:
                        rename = RenameCmd(self, cmd.comp_id, None, self._paste_label)
                        self._execute_cmd(rename)
                    self._update_status(f"Placed {cmd.comp_id}")
                else:
                    self._update_status("Cannot place here - collision or out of bounds")
            self.renderer.redraw()

        elif self.mode == MODE_WIRE:
            if self._wire_start is None:
                self._wire_start = (row, col)
                srl, scl = self._coord_label(row, col)
                self._update_status(f"Wire: from ({srl},{scl}) - click end pad (Esc to cancel)")
            else:
                sr, sc = self._wire_start
                if (sr, sc) != (row, col):
                    cmd = AddGuideCmd(self, sr, sc, row, col, self._wire_color)
                    self._execute_cmd(cmd)
                    srl, scl = self._coord_label(sr, sc)
                    erl, ecl = self._coord_label(row, col)
                    self._update_status(f"Guide: ({srl},{scl}) -> ({erl},{ecl})")
                self._wire_start = None
                self.renderer.guide_preview = None
                self.renderer.redraw()

        elif self.mode == MODE_DELETE:
            # Use float coords for precise proximity detection on lines
            frow, fcol = self.renderer.canvas_to_grid_float(event.x, event.y)
            ferow, fecol = self.renderer.canvas_to_edge_float(event.x, event.y)
            # Check guides and divisions first (thin lines under components)
            idx = self.board.find_guide_near(frow, fcol)
            if idx >= 0:
                cmd = DeleteGuideCmd(self, idx, frow, fcol)
                if self._execute_cmd(cmd):
                    self._update_status("Deleted guide line")
                self.renderer.redraw()
            else:
                didx = self.board.find_division_near(ferow, fecol)
                if didx >= 0:
                    cmd = DeleteDivisionCmd(self, didx)
                    if self._execute_cmd(cmd):
                        self._update_status("Deleted division line")
                    self.renderer.redraw()
                else:
                    # No guide/division near click, try component
                    pc = self.board.get_component_at(row, col)
                    if pc:
                        cmd = DeleteCmd(self, pc.id)
                        if self._execute_cmd(cmd):
                            self._update_status(f"Deleted {pc.id}")
                        self.renderer.redraw()

        elif self.mode == MODE_SELECT:
            # Place pending text label ghost
            if self._ghost_text_label_props is not None:
                row_f, col_f = self.renderer.canvas_to_grid_float(event.x, event.y)
                cmd = AddTextLabelCmd(self, row_f, col_f, self._ghost_text_label_props)
                if self._execute_cmd(cmd):
                    self._select_text_label(cmd.label_id)
                    self._update_status(f"Added text label {cmd.label_id}")
                self._ghost_text_label_props = None
                self.renderer.text_label_ghost = None
                self.renderer.redraw()
                return

            # Hit-test respects visual layer order:
            # 1. 'above' text labels (rendered on top)
            # 2. components
            # 3. 'below' text labels (rendered under components)
            tl = self._get_text_label_at(event.x, event.y, layer='above')
            pc = self.board.get_component_at(row, col) if tl is None else None
            if tl is None and pc is None:
                tl = self._get_text_label_at(event.x, event.y, layer='below')

            if tl:
                self._deselect_text_label()
                self._select_text_label(tl.id)
                self._drag_text_label_id = tl.id
                self._drag_text_label_orig = (tl.row, tl.col)
                rf, cf = self.renderer.canvas_to_grid_float(event.x, event.y)
                self._drag_text_label_click = (rf, cf)
                self._drag_text_label_started = False
                self._drag_start = (event.x, event.y)
                self._update_status(f"Text label {tl.id} — drag to move, double-click to edit")
                self.renderer.redraw()
                return

            if pc:
                self._deselect_text_label()
                ctrl = bool(event.state & 0x4)
                if ctrl:
                    # Ctrl+click: toggle immediately
                    if pc.id in self.selected_comp_ids:
                        self.selected_comp_ids.discard(pc.id)
                        self.renderer.selected_ids.discard(pc.id)
                    else:
                        self.selected_comp_ids.add(pc.id)
                        self.renderer.selected_ids.add(pc.id)
                    self._pending_single_select = None
                elif pc.id in self.selected_comp_ids and len(self.selected_comp_ids) > 1:
                    # Clicking on an already-selected member of a group:
                    # keep full selection for dragging, defer collapse to mouseup
                    self._pending_single_select = pc.id
                else:
                    # Clicking on unselected component: replace selection immediately
                    self._set_selection({pc.id})
                    self._pending_single_select = None
                self.renderer.selected_division = -1
                self._drag_div_idx = -1
                self._drag_start = (event.x, event.y)
                self._drag_comp_id = pc.id
                self._drag_orig_row = pc.anchor_row
                self._drag_orig_col = pc.anchor_col
                self._drag_orig_positions = {
                    cid: (self.board.components[cid].anchor_row,
                          self.board.components[cid].anchor_col)
                    for cid in self.selected_comp_ids if cid in self.board.components
                }
                # Compute drag reference: group centroid (multi) or single center
                if len(self.selected_comp_ids) > 1:
                    all_ctr = [self._component_center(self.board.components[cid])
                               for cid in self.selected_comp_ids
                               if cid in self.board.components]
                    self._drag_orig_center_row = sum(r for r, c in all_ctr) / len(all_ctr)
                    self._drag_orig_center_col = sum(c for r, c in all_ctr) / len(all_ctr)
                else:
                    self._drag_orig_center_row, self._drag_orig_center_col = self._component_center(pc)
                self._drag_started_moving = False
                n = len(self.selected_comp_ids)
                if n > 1:
                    self._update_status(f"{n} components selected")
                else:
                    desc = pc.label or pc.id
                    self._update_status(f"Selected {desc} ({pc.comp_def.name})")
            else:
                # Check for division line near click
                er, ec = self.renderer.canvas_to_edge(event.x, event.y)
                didx = self.board.find_division_near(er, ec)
                if didx >= 0:
                    dl = self.board.divisions[didx]
                    self._drag_div_idx = didx
                    self._drag_div_orig = (dl.r1, dl.c1, dl.r2, dl.c2)
                    self.renderer.selected_division = didx
                    self._clear_selection()
                    self._deselect_text_label()
                    self._drag_comp_id = None
                    self._update_status(f"Selected division {didx} (drag to move)")
                else:
                    # Start rubber-band selection
                    self._clear_selection()
                    self._deselect_text_label()
                    self.renderer.selected_division = -1
                    self._drag_div_idx = -1
                    self._rubberband_start = (event.x, event.y)
                    self._update_status()
            self.renderer.redraw()

    def _clamp_to_board(self, row, col):
        """Clamp row/col to board bounds."""
        return (max(0, min(row, self.board.rows - 1)),
                max(0, min(col, self.board.cols - 1)))

    def _on_drag(self, event):
        if self.mode != MODE_SELECT:
            return
        # Text label drag
        if self._drag_text_label_id:
            if not self._drag_text_label_started:
                sx, sy = self._drag_start
                if (event.x - sx) ** 2 + (event.y - sy) ** 2 < 25:
                    return
                self._drag_text_label_started = True
            tl = self.board.get_text_label(self._drag_text_label_id)
            if tl:
                rf, cf = self.renderer.canvas_to_grid_float(event.x, event.y)
                cr, cc = self._drag_text_label_click
                orig_r, orig_c = self._drag_text_label_orig
                tl.row = orig_r + (rf - cr)
                tl.col = orig_c + (cf - cc)
                self.renderer.redraw()
            return
        # Rubber-band update
        if self._rubberband_start:
            x0, y0 = self._rubberband_start
            self.renderer.selection_rect = (min(x0, event.x), min(y0, event.y),
                                            max(x0, event.x), max(y0, event.y))
            self.renderer.redraw()
            return
        # Drag division line
        if self._drag_div_idx >= 0:
            er, ec = self.renderer.canvas_to_edge(event.x, event.y)
            er, ec = self._clamp_to_edges(er, ec)
            dl = self.board.divisions[self._drag_div_idx]
            orig = self._drag_div_orig
            # Determine if horizontal (r1==r2) or vertical (c1==c2)
            if orig[0] == orig[2]:
                # Horizontal line: shift row
                dl.r1 = er
                dl.r2 = er
            else:
                # Vertical line: shift col
                dl.c1 = ec
                dl.c2 = ec
            self.renderer.redraw()
            return
        # Multi-drag: move all selected components visually (cursor = group centroid)
        if self._drag_comp_id and self._drag_orig_positions:
            self._pending_single_select = None  # drag started, cancel deferred selection
            # 5-pixel dead zone before committing to a drag
            if not self._drag_started_moving:
                sx, sy = self._drag_start
                if (event.x - sx) ** 2 + (event.y - sy) ** 2 < 25:
                    return
                self._drag_started_moving = True
            row_f, col_f = self.renderer.canvas_to_grid_float(event.x, event.y)
            # Delta: cursor represents the group centroid / anchor-component center
            # Use math.floor(x+0.5) instead of round() to avoid Python banker's
            # rounding which causes 2-pad jumps when center is at .5 positions.
            dr = math.floor(row_f - self._drag_orig_center_row + 0.5)
            dc = math.floor(col_f - self._drag_orig_center_col + 0.5)
            for cid, (orig_r, orig_c) in self._drag_orig_positions.items():
                pc = self.board.components.get(cid)
                if pc:
                    nr, nc = self._clamp_to_board(orig_r + dr, orig_c + dc)
                    pc.anchor_row = nr
                    pc.anchor_col = nc
            self.renderer.redraw()
            return
        # Single component drag (fallback — no _drag_orig_positions)
        if self._drag_comp_id is None:
            return
        if not self._drag_started_moving:
            sx, sy = self._drag_start
            if (event.x - sx) ** 2 + (event.y - sy) ** 2 < 25:
                return
            self._drag_started_moving = True
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        row, col = self._clamp_to_board(row, col)
        pc = self.board.components.get(self._drag_comp_id)
        if not pc:
            return
        # Cursor represents visual center
        new_ar, new_ac = self._center_rot_anchor(pc.comp_def, row, col, pc.rotation)
        new_ar, new_ac = self._clamp_to_board(new_ar, new_ac)
        if pc.anchor_row != new_ar or pc.anchor_col != new_ac:
            pc.anchor_row = new_ar
            pc.anchor_col = new_ac
            self.renderer.redraw()

    def _on_release(self, event):
        if self.mode != MODE_SELECT:
            return
        # Finalize text label drag
        if self._drag_text_label_id:
            tl = self.board.get_text_label(self._drag_text_label_id)
            if tl and self._drag_text_label_started:
                orig_r, orig_c = self._drag_text_label_orig
                if tl.row != orig_r or tl.col != orig_c:
                    new_r, new_c = tl.row, tl.col
                    tl.row, tl.col = orig_r, orig_c   # restore for command
                    cmd = MoveTextLabelCmd(self, self._drag_text_label_id,
                                          orig_r, orig_c, new_r, new_c)
                    self._execute_cmd(cmd)
            self._drag_text_label_id = None
            self._drag_text_label_orig = None
            self._drag_text_label_click = None
            self._drag_text_label_started = False
            self.renderer.redraw()
            return
        # Finalize rubber-band selection
        if self._rubberband_start:
            x0, y0 = self._rubberband_start
            x1, y1 = event.x, event.y
            self.renderer.selection_rect = None
            self._rubberband_start = None
            r0f, c0f = self.renderer.canvas_to_grid_float(min(x0, x1), min(y0, y1))
            r1f, c1f = self.renderer.canvas_to_grid_float(max(x0, x1), max(y0, y1))
            selected = set()
            for cid, pc in self.board.components.items():
                for cr, cc in pc.get_all_cells():
                    if r0f - 0.5 <= cr <= r1f + 0.5 and c0f - 0.5 <= cc <= c1f + 0.5:
                        selected.add(cid)
                        break
            self._set_selection(selected)
            if selected:
                n = len(selected)
                self._update_status(f"{n} component(s) selected")
            self.renderer.redraw()
            return
        # Finalize division drag
        if self._drag_div_idx >= 0 and self._drag_div_orig is not None:
            dl = self.board.divisions[self._drag_div_idx]
            new_state = (dl.r1, dl.c1, dl.r2, dl.c2)
            if new_state != self._drag_div_orig:
                # Restore original, then record command
                dl.r1, dl.c1, dl.r2, dl.c2 = self._drag_div_orig
                cmd = MoveDivisionCmd(self, self._drag_div_idx,
                                      *self._drag_div_orig, *new_state)
                self._execute_cmd(cmd)
                self._update_status(f"Moved division {self._drag_div_idx}")
            self.renderer.redraw()
            self._drag_div_idx = -1
            self._drag_div_orig = None
            return
        # Finalize multi-component drag
        if self._drag_comp_id and self._drag_orig_positions:
            anchor_pc = self.board.components.get(self._drag_comp_id)
            if anchor_pc:
                dr = anchor_pc.anchor_row - self._drag_orig_row
                dc = anchor_pc.anchor_col - self._drag_orig_col
                if dr != 0 or dc != 0:
                    # Restore visual positions to originals first
                    for cid, (orig_r, orig_c) in self._drag_orig_positions.items():
                        pc = self.board.components.get(cid)
                        if pc:
                            pc.anchor_row, pc.anchor_col = orig_r, orig_c
                    # Build move entries
                    entries = []
                    for cid, (orig_r, orig_c) in self._drag_orig_positions.items():
                        pc = self.board.components.get(cid)
                        if pc:
                            new_r, new_c = self._clamp_to_board(orig_r + dr, orig_c + dc)
                            entries.append((cid, pc.comp_def, pc.label, pc.rotation,
                                            orig_r, orig_c, new_r, new_c))
                    # Validate: temporarily remove all, check each, restore
                    for cid, *_ in entries:
                        self.board.remove_component(cid)
                    all_valid = all(
                        self.board.can_place(comp_def, new_r, new_c, rot)
                        for _, comp_def, _, rot, _, _, new_r, new_c in entries
                    )
                    for cid, comp_def, label, rot, old_r, old_c, _, _ in entries:
                        placed = self.board.place_component(comp_def, old_r, old_c, rot, comp_id=cid)
                        if placed and label is not None:
                            placed.label = label
                    if all_valid:
                        cmd = MultiMoveCmd(self, entries)
                        self._execute_cmd(cmd)
                        self._update_status(f"Moved {len(entries)} component(s)")
                    else:
                        self._update_status("Cannot move — collision or out of bounds")
            # If no actual drag occurred and selection was deferred, collapse to single
            if self._pending_single_select:
                self._set_selection({self._pending_single_select})
                cid = self._pending_single_select
                pc2 = self.board.components.get(cid)
                if pc2:
                    desc = pc2.label or cid
                    self._update_status(f"Selected {desc} ({pc2.comp_def.name})")
            self._pending_single_select = None
            self._drag_comp_id = None
            self._drag_start = None
            self._drag_orig_positions = {}
            self._drag_orig_center_row = None
            self._drag_orig_center_col = None
            self._drag_started_moving = False
            self.renderer.redraw()
            return
        # Finalize single component drag
        if self._drag_comp_id is None:
            return
        pc = self.board.components.get(self._drag_comp_id)
        if pc and (pc.anchor_row != self._drag_orig_row or pc.anchor_col != self._drag_orig_col):
            # Restore anchor to original (pad grid was never changed during drag)
            final_row, final_col = pc.anchor_row, pc.anchor_col
            pc.anchor_row = self._drag_orig_row
            pc.anchor_col = self._drag_orig_col
            # Try to place at final position via command
            if self.board.can_place(pc.comp_def, final_row, final_col, pc.rotation, exclude_id=pc.id):
                cmd = MoveCmd(self, pc.id, self._drag_orig_row, self._drag_orig_col, final_row, final_col)
                self._execute_cmd(cmd)
                self._update_status(f"Moved {pc.id}")
            else:
                self._update_status("Invalid position - returned to original")
            self.renderer.redraw()
        self._pending_single_select = None
        self._drag_comp_id = None
        self._drag_start = None
        self._drag_orig_center_row = None
        self._drag_orig_center_col = None
        self._drag_started_moving = False

    def _on_mouse_move(self, event):
        if self._ghost_text_label_props is not None:
            row_f, col_f = self.renderer.canvas_to_grid_float(event.x, event.y)
            self.renderer.text_label_ghost_pos = (row_f, col_f)
            self.renderer.redraw()
            return
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        if self._multi_ghost_entries and self.mode == MODE_PLACE:
            row_c, col_c = self._clamp_to_board(row, col)
            dr = row_c - self._multi_ghost_ref_row
            dc = col_c - self._multi_ghost_ref_col
            new_multi_ghost = []
            for e in self._multi_ghost_entries:
                gr, gc = e['base_row'] + dr, e['base_col'] + dc
                valid = self.board.can_place(e['comp_def'], gr, gc, e['new_rot'])
                new_multi_ghost.append((e['comp_def'], gr, gc, e['new_rot'], valid))
            self.renderer.multi_ghost = new_multi_ghost
            self.renderer.redraw()
        elif self.mode == MODE_PLACE and self.place_comp_def:
            row_c, col_c = self._clamp_to_board(row, col)
            # Cursor tracks visual center: compute anchor to center component on cursor
            new_ar, new_ac = self._center_rot_anchor(self.place_comp_def, row_c, col_c, self.place_rotation)
            valid = self.board.can_place(self.place_comp_def, new_ar, new_ac, self.place_rotation)
            self.renderer.ghost = (self.place_comp_def, new_ar, new_ac, self.place_rotation, valid)
            self.renderer.redraw()
        elif self.mode == MODE_WIRE and self._wire_start:
            row_c, col_c = self._clamp_to_board(row, col)
            sr, sc = self._wire_start
            self.renderer.guide_preview = (sr, sc, row_c, col_c)
            self.renderer.redraw()
        elif self.mode == MODE_DIVIDE and self._division_start:
            er, ec = self.renderer.canvas_to_edge(event.x, event.y)
            er, ec = self._clamp_to_edges(er, ec)
            sr, sc = self._division_start
            er, ec = self._snap_division_hv(sr, sc, er, ec)
            self.renderer.division_preview = (sr, sc, er, ec)
            self.renderer.redraw()
        # Update coordinate bar (right side) without overwriting context (left side)
        if 0 <= row < self.board.rows and 0 <= col < self.board.cols:
            rl, cl = self._coord_label(row, col)
            coord_text = f"Row: {rl}  Col: {cl}"
            occ = self.board.pads[row][col].occupied_by
            if occ:
                pc = self.board.components.get(occ)
                if pc:
                    pin_positions = pc.get_pin_positions()
                    for i, (pr, pcol) in enumerate(pin_positions):
                        if pr == row and pcol == col:
                            name = pc.label or occ
                            coord_text += f"  [{name} pin {i + 1}]"
                            break
                    else:
                        coord_text += f"  [{occ}]"
                else:
                    coord_text += f"  [{occ}]"
            self._coord_bar.config(text=coord_text)

    def _on_mousewheel(self, event):
        # Windows: event.delta is +/-120
        if event.delta > 0:
            self.renderer.zoom_in(event.x, event.y)
        else:
            self.renderer.zoom_out(event.x, event.y)

    # ── Free text label helpers ──

    def _get_text_label_at(self, cx, cy, layer=None):
        """Return the TextLabel whose canvas bbox contains (cx,cy), or None.
        layer=None checks all layers; layer='above'/'below' filters by tl.layer."""
        for tl_id, (bx0, by0, bx1, by1) in self.renderer._text_label_bboxes.items():
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                tl = self.board.get_text_label(tl_id)
                if tl and (layer is None or tl.layer == layer):
                    return tl
        return None

    def _select_text_label(self, tl_id):
        self._clear_selection()
        self.renderer.selected_division = -1
        self._selected_text_label_id = tl_id
        self.renderer.selected_text_label_id = tl_id

    def _deselect_text_label(self):
        self._selected_text_label_id = None
        self.renderer.selected_text_label_id = None

    def _add_text_label(self, cx=None, cy=None):
        """Open dialog, then enter ghost mode so the user can position the new label."""
        dlg = TextLabelDialog(self.root, title='Add Text Label')
        if dlg.result and dlg.result['text'].strip():
            if cx is None:
                cx = self.canvas.winfo_width() / 2
                cy = self.canvas.winfo_height() / 2
            row_f, col_f = self.renderer.canvas_to_grid_float(cx, cy)
            self._ghost_text_label_props = dlg.result
            self.renderer.text_label_ghost = dlg.result
            self.renderer.text_label_ghost_pos = (row_f, col_f)
            self._update_status("Click to place text label  (Esc to cancel)")
            self.renderer.redraw()

    def _edit_text_label(self, tl):
        """Open edit dialog for an existing TextLabel."""
        initial = {
            'text': tl.text, 'size': tl.size, 'align': tl.align,
            'opacity': tl.opacity, 'layer': tl.layer, 'color': tl.color,
            'bg_color': tl.bg_color, 'border_color': tl.border_color,
        }
        dlg = TextLabelDialog(self.root, title=f'Edit Text Label {tl.id}', initial=initial)
        if dlg.result is not None:
            old_props = initial.copy()
            new_props = dlg.result
            cmd = EditTextLabelCmd(self, tl.id, old_props, new_props)
            self._execute_cmd(cmd)
            self.renderer.redraw()

    # ── Rotation geometry helpers ──

    def _component_center(self, pc):
        """Return (center_r, center_c) world coords of the component's visual center."""
        all_cells = list(pc.get_all_cells())
        if not all_cells:
            return float(pc.anchor_row), float(pc.anchor_col)
        return (sum(r for r, c in all_cells) / len(all_cells),
                sum(c for r, c in all_cells) / len(all_cells))

    def _center_rot_anchor(self, comp_def, center_r, center_c, new_rot):
        """Compute the anchor that places comp_def's visual center at (center_r, center_c)
        when drawn with rotation new_rot."""
        cells = comp_def.get_rotated_pins(new_rot) + comp_def.get_rotated_body(new_rot)
        if not cells:
            return round(center_r), round(center_c)
        mean_dr = sum(r for r, c in cells) / len(cells)
        mean_dc = sum(c for r, c in cells) / len(cells)
        return math.floor(center_r - mean_dr + 0.5), math.floor(center_c - mean_dc + 0.5)

    # ── Rotate ──

    def _rotate(self):
        if self._selected_text_label_id:
            tl = self.board.get_text_label(self._selected_text_label_id)
            if tl:
                new_rot = (tl.rotation + 90) % 360
                cmd = RotateTextLabelCmd(self, tl.id, tl.rotation, new_rot)
                self._execute_cmd(cmd)
                self._update_status(f"Rotated text label {tl.id} to {new_rot}°")
                self.renderer.redraw()
            return
        if self.mode == MODE_PLACE and self._multi_ghost_entries:
            # Rotate all ghost components another 90° around group centroid
            cr = self._multi_ghost_ref_row
            cc = self._multi_ghost_ref_col
            for e in self._multi_ghost_entries:
                dr = e['base_row'] - cr
                dc = e['base_col'] - cc
                e['base_row'] = round(cr + dc)
                e['base_col'] = round(cc - dr)
                e['new_rot'] = (e['new_rot'] + 90) % 360
            self.renderer.multi_ghost = [
                (e['comp_def'], e['base_row'], e['base_col'], e['new_rot'],
                 self.board.can_place(e['comp_def'], e['base_row'], e['base_col'], e['new_rot']))
                for e in self._multi_ghost_entries
            ]
            self.renderer.redraw()
        elif self.mode == MODE_PLACE:
            if self.renderer.ghost:
                comp_def, row, col, old_rot, _ = self.renderer.ghost
                new_rot = (old_rot + 90) % 360
                # Keep visual center fixed: compute center at old anchor/rotation
                old_cells = comp_def.get_rotated_pins(old_rot) + comp_def.get_rotated_body(old_rot)
                if old_cells:
                    ctr_r = row + sum(r for r, c in old_cells) / len(old_cells)
                    ctr_c = col + sum(c for r, c in old_cells) / len(old_cells)
                else:
                    ctr_r, ctr_c = float(row), float(col)
                new_ar, new_ac = self._center_rot_anchor(comp_def, ctr_r, ctr_c, new_rot)
                valid = self.board.can_place(comp_def, new_ar, new_ac, new_rot)
                self.renderer.ghost = (comp_def, new_ar, new_ac, new_rot, valid)
            self.place_rotation = (self.place_rotation + 90) % 360
            self._update_status(f"Rotation: {self.place_rotation}")
            self.renderer.redraw()
        elif self.mode == MODE_SELECT and self.selected_comp_ids:
            if len(self.selected_comp_ids) == 1:
                cid = next(iter(self.selected_comp_ids))
                pc = self.board.components.get(cid)
                if not pc:
                    return
                new_rot = (pc.rotation + 90) % 360
                # Compute anchor that preserves the component's visual center
                ctr_r, ctr_c = self._component_center(pc)
                new_ar, new_ac = self._center_rot_anchor(pc.comp_def, ctr_r, ctr_c, new_rot)
                if self.board.can_place(pc.comp_def, new_ar, new_ac, new_rot, exclude_id=cid):
                    entry = (cid, pc.comp_def, pc.label,
                             pc.anchor_row, pc.anchor_col, pc.rotation,
                             new_ar, new_ac, new_rot)
                    self._execute_cmd(MultiRotateCmd(self, [entry]))
                    self._update_status(f"Rotated {cid}")
                else:
                    self._lift_for_rotation(cid, ghost_row=new_ar, ghost_col=new_ac)
            else:
                # Multi-rotate: rotate each component's visual center around group centroid
                comp_ids = list(self.selected_comp_ids)
                valid_pcs = [(cid, self.board.components[cid])
                             for cid in comp_ids if cid in self.board.components]
                if not valid_pcs:
                    return
                # Compute visual center of each component and group centroid
                comp_centers = {cid: self._component_center(pc) for cid, pc in valid_pcs}
                all_cr = [r for r, c in comp_centers.values()]
                all_cc = [c for r, c in comp_centers.values()]
                gr_cr = sum(all_cr) / len(all_cr)
                gr_cc = sum(all_cc) / len(all_cc)
                # Temporarily remove all to check placement
                saved = {}
                for cid, pc in valid_pcs:
                    saved[cid] = (pc.comp_def, pc.label, pc.anchor_row, pc.anchor_col, pc.rotation)
                    self.board.remove_component(cid)
                # Compute new position for each: rotate its visual center around group centroid
                new_states = {}
                for cid, pc in valid_pcs:
                    comp_def, label, old_r, old_c, old_rot = saved[cid]
                    dr = comp_centers[cid][0] - gr_cr
                    dc = comp_centers[cid][1] - gr_cc
                    new_ctr_r = gr_cr + dc   # 90° CW: (dr,dc) → (dc,-dr)
                    new_ctr_c = gr_cc - dr
                    new_rot = (old_rot + 90) % 360
                    new_ar, new_ac = self._center_rot_anchor(comp_def, new_ctr_r, new_ctr_c, new_rot)
                    new_states[cid] = (new_ar, new_ac, new_rot)
                can_all_fit = all(
                    self.board.can_place(saved[cid][0], ns[0], ns[1], ns[2])
                    for cid, ns in new_states.items()
                )
                # Restore all to original positions
                for cid, (comp_def, label, old_r, old_c, old_rot) in saved.items():
                    placed = self.board.place_component(comp_def, old_r, old_c, old_rot, comp_id=cid)
                    if placed and label is not None:
                        placed.label = label
                if can_all_fit:
                    rot_entries = [
                        (cid, saved[cid][0], saved[cid][1],
                         saved[cid][2], saved[cid][3], saved[cid][4],
                         new_states[cid][0], new_states[cid][1], new_states[cid][2])
                        for cid in comp_ids if cid in new_states
                    ]
                    self._execute_cmd(MultiRotateCmd(self, rot_entries))
                    self._update_status(f"Rotated {len(comp_ids)} component(s)")
                else:
                    self._enter_multi_ghost(comp_ids, new_positions=new_states)
            self.renderer.redraw()

    def _lift_for_rotation(self, comp_id, ghost_row=None, ghost_col=None):
        """Remove component from board and enter floating place mode so the user
        can choose where to drop it at its new rotation.

        ghost_row/col: initial ghost position (defaults to center-preserving position,
        then falls back to original anchor).
        """
        pc = self.board.components.get(comp_id)
        if not pc:
            return
        self._lifted_comp_id = pc.id
        self._lifted_comp_def = pc.comp_def
        self._lifted_comp_label = pc.label
        self._lifted_orig_row = pc.anchor_row
        self._lifted_orig_col = pc.anchor_col
        self._lifted_orig_rot = pc.rotation
        self.board.remove_component(pc.id)
        self.place_comp_def = pc.comp_def
        self.place_rotation = (pc.rotation + 90) % 360
        self._paste_label = None
        self.mode = MODE_PLACE
        self._mode_var.set(MODE_PLACE)
        # Show ghost at the specified position (center-preserving if provided)
        new_rot = self.place_rotation
        gr = ghost_row if ghost_row is not None else self._lifted_orig_row
        gc = ghost_col if ghost_col is not None else self._lifted_orig_col
        valid = self.board.can_place(pc.comp_def, gr, gc, new_rot)
        self.renderer.ghost = (pc.comp_def, gr, gc, new_rot, valid)
        self._update_status(f"Rotate {self._lifted_comp_id}: choose position (Esc to cancel)")

    def _clear_lifted(self):
        self._lifted_comp_id = None
        self._lifted_comp_def = None
        self._lifted_comp_label = None
        self._lifted_orig_row = None
        self._lifted_orig_col = None
        self._lifted_orig_rot = None

    def _restore_lifted(self):
        """Put a lifted component back at its original position (used on Esc / mode switch)."""
        if not self._lifted_comp_id:
            return
        placed = self.board.place_component(
            self._lifted_comp_def,
            self._lifted_orig_row, self._lifted_orig_col, self._lifted_orig_rot,
            comp_id=self._lifted_comp_id,
        )
        if placed and self._lifted_comp_label is not None:
            placed.label = self._lifted_comp_label
        self._clear_lifted()

    # ── Selection helpers ──

    def _set_selection(self, comp_ids):
        """Set the selection to exactly the given set of component IDs."""
        self.selected_comp_ids = set(comp_ids)
        self.renderer.selected_ids = set(comp_ids)

    def _clear_selection(self):
        """Clear the current selection."""
        self.selected_comp_ids = set()
        self.renderer.selected_ids = set()

    def _select_all(self):
        """Select all components on the board (Ctrl+A)."""
        if self.mode == MODE_SELECT:
            self._set_selection(set(self.board.components.keys()))
            n = len(self.selected_comp_ids)
            if n:
                self._update_status(f"{n} components selected")
            self.renderer.redraw()

    # ── Multi-ghost helpers ──

    def _enter_multi_ghost(self, comp_ids, new_rot_delta=90, new_positions=None):
        """Remove all listed components and enter multi-ghost floating mode.

        new_positions: optional {comp_id: (new_r, new_c, new_rot)} for pre-computed
                       center-preserving positions. If None, uses original anchors.
        """
        entries = []
        for cid in comp_ids:
            pc = self.board.components.get(cid)
            if pc:
                if new_positions and cid in new_positions:
                    base_r, base_c, new_rot = new_positions[cid]
                else:
                    base_r, base_c = pc.anchor_row, pc.anchor_col
                    new_rot = (pc.rotation + new_rot_delta) % 360
                entries.append({
                    'comp_id': cid,
                    'comp_def': pc.comp_def,
                    'label': pc.label,
                    'old_row': pc.anchor_row,
                    'old_col': pc.anchor_col,
                    'old_rot': pc.rotation,
                    'new_rot': new_rot,
                    'base_row': base_r,
                    'base_col': base_c,
                })
                self.board.remove_component(cid)
        if not entries:
            return
        self._multi_ghost_entries = entries
        # Compute group centroid as reference point
        self._multi_ghost_ref_row = sum(e['base_row'] for e in entries) / len(entries)
        self._multi_ghost_ref_col = sum(e['base_col'] for e in entries) / len(entries)
        # Round to nearest grid position for cursor tracking
        self._multi_ghost_ref_row = round(self._multi_ghost_ref_row)
        self._multi_ghost_ref_col = round(self._multi_ghost_ref_col)
        # Show initial ghost at base positions (delta=0)
        self.renderer.multi_ghost = [
            (e['comp_def'], e['base_row'], e['base_col'], e['new_rot'],
             self.board.can_place(e['comp_def'], e['base_row'], e['base_col'], e['new_rot']))
            for e in entries
        ]
        self.place_comp_def = None
        self.mode = MODE_PLACE
        self._mode_var.set(MODE_PLACE)
        self._update_status(
            f"Rotate group: choose position (R to rotate more, Esc to cancel)"
        )

    def _clear_multi_ghost(self):
        """Clear multi-ghost state without restoring components."""
        self._multi_ghost_entries = []
        self._multi_ghost_ref_row = None
        self._multi_ghost_ref_col = None
        self.renderer.multi_ghost = None

    def _restore_multi_ghost(self):
        """Restore all multi-ghost components to their original positions and clear state."""
        for e in self._multi_ghost_entries:
            placed = self.board.place_component(
                e['comp_def'], e['old_row'], e['old_col'], e['old_rot'],
                comp_id=e['comp_id']
            )
            if placed and e['label'] is not None:
                placed.label = e['label']
        self._clear_multi_ghost()

    # ── Undo/Redo ──

    def _execute_cmd(self, cmd):
        if cmd.execute():
            self._undo_stack.append(cmd)
            self._redo_stack.clear()
            self.modified = True
            self._update_title()
            return True
        return False

    def _undo(self):
        if not self._undo_stack:
            return
        cmd = self._undo_stack.pop()
        cmd.undo()
        self._redo_stack.append(cmd)
        self.modified = True
        self._update_title()
        self.renderer.redraw()
        self._update_status("Undo")

    def _redo(self):
        if not self._redo_stack:
            return
        cmd = self._redo_stack.pop()
        cmd.execute()
        self._undo_stack.append(cmd)
        self.modified = True
        self._update_title()
        self.renderer.redraw()
        self._update_status("Redo")

    # ── Delete ──

    def _delete_selected(self):
        if self._selected_text_label_id:
            cmd = DeleteTextLabelCmd(self, self._selected_text_label_id)
            if self._execute_cmd(cmd):
                self._deselect_text_label()
                self._update_status("Deleted text label")
                self.renderer.redraw()
            return
        if not self.selected_comp_ids:
            return
        n = len(self.selected_comp_ids)
        if n == 1:
            cid = next(iter(self.selected_comp_ids))
            cmd = DeleteCmd(self, cid)
        else:
            cmd = MultiDeleteCmd(self, self.selected_comp_ids)
        if self._execute_cmd(cmd):
            self._clear_selection()
            self._update_status(f"Deleted {n} component(s)")
            self.renderer.redraw()

    def _move_selected(self, dr, dc):
        """Move selected component(s) or division by (dr, dc) using arrow keys."""
        if self.mode != MODE_SELECT:
            return
        # Move division line
        if self.renderer.selected_division >= 0:
            idx = self.renderer.selected_division
            if idx >= len(self.board.divisions):
                return
            dl = self.board.divisions[idx]
            nr1, nc1 = dl.r1 + dr, dl.c1 + dc
            nr2, nc2 = dl.r2 + dr, dl.c2 + dc
            # Clamp to edge bounds [0, rows] x [0, cols]
            if not (0 <= nr1 <= self.board.rows and 0 <= nr2 <= self.board.rows and
                    0 <= nc1 <= self.board.cols and 0 <= nc2 <= self.board.cols):
                return
            old = (dl.r1, dl.c1, dl.r2, dl.c2)
            cmd = MoveDivisionCmd(self, idx, *old, nr1, nc1, nr2, nc2)
            self._execute_cmd(cmd)
            self.renderer.redraw()
            return
        # Move selected components
        if not self.selected_comp_ids:
            return
        if len(self.selected_comp_ids) == 1:
            cid = next(iter(self.selected_comp_ids))
            pc = self.board.components.get(cid)
            if not pc:
                return
            new_row = pc.anchor_row + dr
            new_col = pc.anchor_col + dc
            if self.board.can_place(pc.comp_def, new_row, new_col, pc.rotation, exclude_id=pc.id):
                cmd = MoveCmd(self, pc.id, pc.anchor_row, pc.anchor_col, new_row, new_col)
                self._execute_cmd(cmd)
                self.renderer.redraw()
        else:
            # Multi-move: build entries and validate
            comp_ids = list(self.selected_comp_ids)
            entries = []
            for cid in comp_ids:
                pc = self.board.components.get(cid)
                if pc:
                    new_r = pc.anchor_row + dr
                    new_c = pc.anchor_col + dc
                    entries.append((cid, pc.comp_def, pc.label, pc.rotation,
                                    pc.anchor_row, pc.anchor_col, new_r, new_c))
            if not entries:
                return
            # Validate: temporarily remove all, check each, restore
            for cid, *_ in entries:
                self.board.remove_component(cid)
            all_valid = all(
                self.board.can_place(comp_def, new_r, new_c, rot)
                for _, comp_def, _, rot, _, _, new_r, new_c in entries
            )
            for cid, comp_def, label, rot, old_r, old_c, _, _ in entries:
                placed = self.board.place_component(comp_def, old_r, old_c, rot, comp_id=cid)
                if placed and label is not None:
                    placed.label = label
            if all_valid:
                cmd = MultiMoveCmd(self, entries)
                self._execute_cmd(cmd)
                self.renderer.redraw()

    # ── Clipboard (Cut/Copy/Paste) ──

    def _copy_selected(self):
        """Copy selected component to system clipboard (single selection only)."""
        if len(self.selected_comp_ids) != 1:
            return
        cid = next(iter(self.selected_comp_ids))
        pc = self.board.components.get(cid)
        if not pc:
            return
        data = pc.to_dict()
        clip_text = CLIPBOARD_PREFIX + json.dumps(data)
        self.root.clipboard_clear()
        self.root.clipboard_append(clip_text)
        label = pc.label or pc.id
        self._update_status(f"Copied {pc.id} ({label})")

    def _cut_selected(self):
        """Cut selected component: copy to clipboard + delete (single selection only)."""
        if len(self.selected_comp_ids) != 1:
            return
        cid = next(iter(self.selected_comp_ids))
        pc = self.board.components.get(cid)
        if not pc:
            return
        # Copy to clipboard first
        data = pc.to_dict()
        clip_text = CLIPBOARD_PREFIX + json.dumps(data)
        self.root.clipboard_clear()
        self.root.clipboard_append(clip_text)
        label = pc.label or pc.id
        # Delete via command (supports undo)
        cmd = DeleteCmd(self, cid)
        self._execute_cmd(cmd)
        self._clear_selection()
        self.renderer.redraw()
        self._update_status(f"Cut {pc.id} ({label})")

    def _paste_clipboard(self):
        """Paste component from system clipboard: enter PLACE mode with ghost."""
        try:
            clip_text = self.root.clipboard_get()
        except tk.TclError:
            return  # clipboard empty or unavailable
        if not clip_text.startswith(CLIPBOARD_PREFIX):
            return
        json_str = clip_text[len(CLIPBOARD_PREFIX):]
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return
        type_id = data.get('type')
        if not type_id:
            return
        comp_def = self.library.get(type_id)
        if not comp_def:
            self._update_status(f"Unknown component type: {type_id}")
            return
        # Enter PLACE mode with rotation and label from clipboard
        self.place_comp_def = comp_def
        self.place_rotation = data.get('rotation', 0)
        self._paste_label = data.get('label')
        self._set_mode(MODE_PLACE)
        self._update_status(f"Paste: {comp_def.name} (click to place)")
        self.canvas.focus_set()

    # ── File operations ──

    def _get_hwnd(self):
        """Get the native window handle for lock files."""
        try:
            frame = self.root.wm_frame()
            return int(frame, 16) if frame else self.root.winfo_id()
        except Exception:
            return self.root.winfo_id()

    def _acquire_lock(self):
        """Acquire lock for current self.filepath."""
        if self.filepath:
            write_lock(self.filepath, self._get_hwnd())
            self._file_lock_path = self.filepath

    def _release_lock(self):
        """Release lock for the currently locked file."""
        if self._file_lock_path:
            release_lock(self._file_lock_path)
            self._file_lock_path = None

    def _new_board(self):
        if self.modified:
            choice = self._ask_save_changes("New Board")
            if choice == 'cancel':
                return
            if choice == 'save':
                self._save_file()
                if self.modified:
                    return

        dialog = tk.Toplevel(self.root)
        dialog.title("New Board")
        dialog.geometry("300x220")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="Board Preset:").pack(anchor='w', padx=10, pady=(10, 2))
        preset_var = tk.StringVar(value='SBC-WALL (74x57)')
        preset_combo = ttk.Combobox(dialog, textvariable=preset_var,
                                     values=list(BOARD_PRESETS.keys()) + ['Custom'],
                                     state='readonly')
        preset_combo.pack(fill=tk.X, padx=10)

        custom_frame = ttk.Frame(dialog)
        custom_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(custom_frame, text="Rows:").grid(row=0, column=0, padx=2)
        rows_var = tk.StringVar(value='57')
        rows_entry = ttk.Entry(custom_frame, textvariable=rows_var, width=8)
        rows_entry.grid(row=0, column=1, padx=2)
        ttk.Label(custom_frame, text="Cols:").grid(row=0, column=2, padx=2)
        cols_var = tk.StringVar(value='74')
        cols_entry = ttk.Entry(custom_frame, textvariable=cols_var, width=8)
        cols_entry.grid(row=0, column=3, padx=2)

        def on_preset(event=None):
            key = preset_var.get()
            if key in BOARD_PRESETS:
                r, c = BOARD_PRESETS[key]
                rows_var.set(str(r))
                cols_var.set(str(c))

        preset_combo.bind('<<ComboboxSelected>>', on_preset)

        ttk.Label(dialog, text="Title:").pack(anchor='w', padx=10, pady=(5, 2))
        title_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=title_var).pack(fill=tk.X, padx=10)

        def do_create():
            try:
                rows = int(rows_var.get())
                cols = int(cols_var.get())
                if rows < 5 or cols < 5 or rows > 200 or cols > 200:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Error", "Rows/cols must be 5-200")
                return
            self._release_lock()
            self.board = Board(rows, cols)
            self.board.title = title_var.get()
            self.renderer.board = self.board
            self.filepath = None
            self.modified = False
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._clear_selection()
            self.renderer.ghost = None
            self.renderer.multi_ghost = None
            self._update_title()
            self.renderer.zoom_fit()
            dialog.destroy()

        ttk.Button(dialog, text="Create", command=do_create).pack(pady=10)

    def _startup_load(self):
        """On startup: load recent file if no file loaded yet, then zoom fit."""
        if self.filepath is None:
            self._load_recent()
        # Delay zoom_fit so canvas has its final size (after maximize, etc.)
        self.root.after(50, self.renderer.zoom_fit)

    def _save_recent(self, filepath):
        """Remember last opened/saved file path."""
        try:
            with open(self._recent_path, 'w', encoding='utf-8') as f:
                f.write(os.path.abspath(filepath))
        except OSError:
            pass

    def _load_recent(self):
        """Load last file on startup. Called via root.after()."""
        try:
            with open(self._recent_path, 'r', encoding='utf-8') as f:
                filepath = f.read().strip()
            if filepath and os.path.isfile(filepath):
                locked, _ = is_locked_by_other(filepath)
                if locked:
                    return  # another instance has it open, stay on empty board
                self.board = load_board(filepath, self.library)
                self.renderer.board = self.board
                self._apply_label_config()
                self.filepath = filepath
                self.modified = False
                self._acquire_lock()
                self._update_title()
                self.renderer.zoom_fit()
                self._update_status(f"Loaded: {os.path.basename(filepath)}")
        except (OSError, Exception):
            pass

    def _save_geometry(self):
        """Save window state and geometry. When maximized, save the normal
        geometry (for un-maximize) plus the 'zoomed' state flag."""
        try:
            state = self.root.state()  # 'zoomed', 'normal', 'iconic'
            if state == 'zoomed':
                # While maximized, .geometry() returns the maximized size.
                # Temporarily un-maximize to capture the normal geometry.
                self.root.state('normal')
                self.root.update_idletasks()
                geo = self.root.geometry()
            else:
                geo = self.root.geometry()
            with open(self._geometry_path, 'w', encoding='utf-8') as f:
                f.write(f"{state}\n{geo}")
        except OSError:
            pass

    def _restore_geometry(self):
        """Restore window geometry and state from last session, with safety checks."""
        try:
            with open(self._geometry_path, 'r', encoding='utf-8') as f:
                lines = f.read().strip().splitlines()
            if not lines:
                return
            # Format: line 0 = state, line 1 = geometry (backwards compat: single line = geo only)
            if len(lines) >= 2:
                state = lines[0].strip()
                geo = lines[1].strip()
            else:
                state = 'normal'
                geo = lines[0].strip()
            if not geo:
                return
            # Parse WxH+X+Y (X/Y can be negative)
            size_part, rest = geo.split('+', 1) if '+' in geo else (geo, None)
            if not rest:
                return
            w, h = (int(v) for v in size_part.split('x'))
            parts = rest.replace('+-', '+NEG').split('+')
            x = int(parts[0].replace('NEG', '-'))
            y = int(parts[1].replace('NEG', '-')) if len(parts) > 1 else 0
            # Sanity: minimum window size
            w = max(400, w)
            h = max(300, h)
            # Check if window is at least partially visible on current screens
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            if x + w < 50 or y + h < 50 or x > screen_w - 50 or y > screen_h - 50:
                # Off-screen: only maximize if that was the state, skip position
                if state == 'zoomed':
                    self.root.state('zoomed')
                return
            # Restore normal geometry first, then maximize if needed
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            if state == 'zoomed':
                self.root.state('zoomed')
        except (OSError, ValueError):
            pass

    def _open_file(self):
        if self.modified:
            choice = self._ask_save_changes("Open")
            if choice == 'cancel':
                return
            if choice == 'save':
                self._save_file()
                if self.modified:
                    return
        filepath = filedialog.askopenfilename(
            filetypes=[("Breadboard Sim", "*.bbsim"), ("All files", "*.*")],
            defaultextension=".bbsim"
        )
        if not filepath:
            return
        filepath = os.path.abspath(filepath)
        locked, _ = is_locked_by_other(filepath)
        if locked:
            messagebox.showwarning("File In Use",
                                   f"{os.path.basename(filepath)} is open in another instance.")
            return
        try:
            self._release_lock()
            self.board = load_board(filepath, self.library)
            self.renderer.board = self.board
            self._apply_label_config()
            self.filepath = filepath
            self.modified = False
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._clear_selection()
            self.renderer.ghost = None
            self.renderer.multi_ghost = None
            self._acquire_lock()
            self._save_recent(filepath)
            self._update_title()
            self.renderer.zoom_fit()
            self._update_status(f"Loaded: {os.path.basename(filepath)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load:\n{e}")

    def _save_file(self):
        if self.filepath:
            self._do_save(self.filepath)
        else:
            self._save_as()

    def _save_as(self):
        filepath = filedialog.asksaveasfilename(
            filetypes=[("Breadboard Sim", "*.bbsim"), ("All files", "*.*")],
            defaultextension=".bbsim"
        )
        if filepath:
            self._do_save(filepath)

    def _do_save(self, filepath):
        try:
            filepath = os.path.abspath(filepath)
            save_board(self.board, filepath, self.library)
            # If filepath changed (Save As), transfer lock
            old_path = self.filepath
            self.filepath = filepath
            self.modified = False
            if old_path != filepath:
                self._release_lock()
                self._acquire_lock()
            self._save_recent(filepath)
            self._update_title()
            self._update_status(f"Saved: {os.path.basename(filepath)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{e}")

    def _export_png(self):
        filepath = filedialog.asksaveasfilename(
            filetypes=[("PNG Image", "*.png"), ("PostScript", "*.ps")],
            defaultextension=".png"
        )
        if filepath:
            ok = self.renderer.export_png(filepath)
            if ok:
                self._update_status(f"Exported: {os.path.basename(filepath)}")
            else:
                self._update_status(f"Exported as PostScript (install Pillow for PNG)")

    def _ask_save_changes(self, title="Unsaved Changes"):
        """Ask user to Save / Don't Save / Cancel. Returns 'save', 'discard', or 'cancel'."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        result = ['cancel']
        ttk.Label(dlg, text="Save changes before continuing?").pack(padx=20, pady=(15, 10))
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(padx=10, pady=(0, 12))
        def pick(val):
            result[0] = val
            dlg.destroy()
        ttk.Button(btn_frame, text="Save", command=lambda: pick('save')).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Don't Save", command=lambda: pick('discard')).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Cancel", command=lambda: pick('cancel')).pack(side=tk.LEFT, padx=4)
        dlg.protocol('WM_DELETE_WINDOW', lambda: pick('cancel'))
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.wait_window()
        return result[0]

    def _quit(self):
        if self.modified:
            choice = self._ask_save_changes("Quit")
            if choice == 'cancel':
                return
            if choice == 'save':
                self._save_file()
                if self.modified:  # save failed or was cancelled
                    return
        self._release_lock()
        self._save_geometry()
        self.root.withdraw()
        self.root.quit()

    # ── View ──

    def _toggle_grid(self):
        self.renderer.show_grid = self._show_grid_var.get()
        self.renderer.redraw()

    def _toggle_labels(self):
        self.renderer.show_labels = self._show_labels_var.get()
        self.renderer.redraw()

    def _update_label_config(self):
        self.renderer.row_label_mode = self._row_mode_var.get()
        self.renderer.col_label_mode = self._col_mode_var.get()
        self.renderer.row_label_dir = self._row_dir_var.get()
        self.renderer.col_label_dir = self._col_dir_var.get()
        self.board.label_config = {
            'row_mode': self.renderer.row_label_mode,
            'col_mode': self.renderer.col_label_mode,
            'row_dir': self.renderer.row_label_dir,
            'col_dir': self.renderer.col_label_dir,
        }
        self.modified = True
        self._update_title()
        self.renderer.redraw()

    def _apply_label_config(self):
        """Apply board's label config to renderer and menu vars."""
        cfg = self.board.label_config
        if cfg:
            self.renderer.row_label_mode = cfg.get('row_mode', 'num')
            self.renderer.col_label_mode = cfg.get('col_mode', 'num')
            self.renderer.row_label_dir = cfg.get('row_dir', 'asc')
            self.renderer.col_label_dir = cfg.get('col_dir', 'asc')
            self._row_mode_var.set(self.renderer.row_label_mode)
            self._col_mode_var.set(self.renderer.col_label_mode)
            self._row_dir_var.set(self.renderer.row_label_dir)
            self._col_dir_var.set(self.renderer.col_label_dir)

    # ── Board operations ──

    def _rotate_board(self):
        self.board.rotate_board_cw()
        self._clear_selection()
        self.renderer.ghost = None
        self.renderer.multi_ghost = None
        self.modified = True
        self._update_title()
        self.renderer.zoom_fit()
        self._update_status(f"Board rotated 90\u00b0 CW ({self.board.rows}x{self.board.cols})")

    def _resize_board(self):
        rows = simpledialog.askinteger("Resize", "Rows:", initialvalue=self.board.rows,
                                        minvalue=5, maxvalue=200, parent=self.root)
        if rows is None:
            return
        cols = simpledialog.askinteger("Resize", "Cols:", initialvalue=self.board.cols,
                                        minvalue=5, maxvalue=200, parent=self.root)
        if cols is None:
            return
        self.board.resize(rows, cols)
        self.modified = True
        self._update_title()
        self.renderer.zoom_fit()

    def _clear_board(self):
        if self.board.components:
            if not messagebox.askyesno("Clear", "Remove all components?"):
                return
        self.board.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._clear_selection()
        self.modified = True
        self._update_title()
        self.renderer.redraw()

    def _board_info(self):
        info = (
            f"Board: {self.board.rows} rows x {self.board.cols} cols\n"
            f"Title: {self.board.title or '(untitled)'}\n"
            f"Components: {len(self.board.components)}\n"
            f"Guides: {len(self.board.guides)}\n"
            f"File: {self.filepath or '(unsaved)'}"
        )
        messagebox.showinfo("Board Info", info)

    # ── Helpers ──

    def _update_title(self):
        name = os.path.basename(self.filepath) if self.filepath else "Untitled"
        mod = " *" if self.modified else ""
        self.root.title(f"Breadboard Simulator - {name}{mod}")

    def _coord_label(self, row, col):
        """Return formatted (row_label, col_label) using current config."""
        rl = self.renderer._grid_label(row, self.board.rows,
                                       self.renderer.row_label_mode, self.renderer.row_label_dir)
        cl = self.renderer._grid_label(col, self.board.cols,
                                       self.renderer.col_label_mode, self.renderer.col_label_dir)
        return rl, cl

    def _update_status(self, text=None):
        if not text:
            mode_text = {'SELECT': 'Select mode', 'PLACE': 'Place mode',
                         'WIRE': 'Wire mode - click start pad',
                         'DIVIDE': 'Divide mode - click start edge',
                         'DELETE': 'Delete mode'}
            text = mode_text.get(self.mode, 'Ready')
        # Pad to exactly 2 lines so the statusbar height stays fixed
        lines = text.split('\n')
        while len(lines) < 2:
            lines.append('')
        self.statusbar.config(text='\n'.join(lines[:2]))

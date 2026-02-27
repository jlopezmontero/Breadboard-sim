"""
Main GUI window: palette, toolbar, canvas, statusbar, interaction modes.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import json
import math
import os
import sys

from board import Board
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
    def __init__(self, app, comp_id, old_label, new_label):
        self.app = app
        self.comp_id = comp_id
        self.old_label = old_label
        self.new_label = new_label

    def execute(self):
        pc = self.app.board.components.get(self.comp_id)
        if pc:
            pc.label = self.new_label
            return True
        return False

    def undo(self):
        pc = self.app.board.components.get(self.comp_id)
        if pc:
            pc.label = self.old_label


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
        self.selected_comp_id = None
        self._drag_start = None
        self._drag_comp_id = None
        self._drag_orig_row = None
        self._drag_orig_col = None
        self._paste_label = None     # label to apply after paste-place
        self._wire_color = '#FFFFFF'  # currently selected wire color
        self._wire_start = None      # (row, col) of wire start pad
        self._division_start = None  # (row, col) of division start edge point
        self._drag_div_idx = -1      # index of division being dragged
        self._drag_div_orig = None   # (r1, c1, r2, c2) before drag
        self._file_lock_path = None  # filepath currently holding our lock

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
        self.statusbar = ttk.Label(status_frame, text="Ready", anchor='w')
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

    # ── Mode handling ──

    def _set_mode(self, mode):
        """Switch to a mode programmatically (from keyboard shortcut)."""
        self._mode_var.set(mode)
        self._mode_changed()

    def _mode_changed(self):
        self.mode = self._mode_var.get()
        if self.mode != MODE_PLACE:
            self.renderer.ghost = None
            self._paste_label = None
        if self.mode != MODE_WIRE:
            self._wire_start = None
            self.renderer.guide_preview = None
        if self.mode != MODE_DIVIDE:
            self._division_start = None
            self.renderer.division_preview = None
        if self.mode != MODE_SELECT:
            self.selected_comp_id = None
            self.renderer.selected_id = None
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
            self.mode = MODE_SELECT
            self._mode_var.set(MODE_SELECT)
            self.renderer.ghost = None
            self.renderer.redraw()
        self.selected_comp_id = None
        self.renderer.selected_id = None
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
        """Double-click on a component to rename it."""
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        pc = self.board.get_component_at(row, col)
        if pc is None:
            return
        current = pc.label or pc.id
        new_label = simpledialog.askstring(
            "Rename Component",
            f"Label for {pc.id} ({pc.comp_def.name}):",
            initialvalue=current,
            parent=self.root
        )
        if new_label is not None:
            new_label = new_label.strip() or None
            old_label = pc.label
            if new_label != old_label:
                cmd = RenameCmd(self, pc.id, old_label, new_label)
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

        elif self.mode == MODE_PLACE and self.place_comp_def:
            cmd = PlaceCmd(self, self.place_comp_def, row, col, self.place_rotation)
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
            pc = self.board.get_component_at(row, col)
            if pc:
                self.selected_comp_id = pc.id
                self.renderer.selected_id = pc.id
                self.renderer.selected_division = -1
                self._drag_div_idx = -1
                self._drag_start = (event.x, event.y)
                self._drag_comp_id = pc.id
                self._drag_orig_row = pc.anchor_row
                self._drag_orig_col = pc.anchor_col
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
                    self.selected_comp_id = None
                    self.renderer.selected_id = None
                    self._drag_comp_id = None
                    self._update_status(f"Selected division {didx} (drag to move)")
                else:
                    self.selected_comp_id = None
                    self.renderer.selected_id = None
                    self.renderer.selected_division = -1
                    self._drag_div_idx = -1
                    self._update_status()
            self.renderer.redraw()

    def _clamp_to_board(self, row, col):
        """Clamp row/col to board bounds."""
        return (max(0, min(row, self.board.rows - 1)),
                max(0, min(col, self.board.cols - 1)))

    def _on_drag(self, event):
        if self.mode != MODE_SELECT:
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
        # Drag component
        if self._drag_comp_id is None:
            return
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        row, col = self._clamp_to_board(row, col)
        pc = self.board.components.get(self._drag_comp_id)
        if not pc:
            return
        if pc.anchor_row != row or pc.anchor_col != col:
            # Visual-only move during drag: just update anchor, don't touch pad grid
            pc.anchor_row = row
            pc.anchor_col = col
            self.renderer.redraw()

    def _on_release(self, event):
        if self.mode != MODE_SELECT:
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
        # Finalize component drag
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
        self._drag_comp_id = None
        self._drag_start = None

    def _on_mouse_move(self, event):
        row, col = self.renderer.canvas_to_grid(event.x, event.y)
        if self.mode == MODE_PLACE and self.place_comp_def:
            row_c, col_c = self._clamp_to_board(row, col)
            valid = self.board.can_place(self.place_comp_def, row_c, col_c, self.place_rotation)
            self.renderer.ghost = (self.place_comp_def, row_c, col_c, self.place_rotation, valid)
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

    # ── Rotate ──

    def _rotate(self):
        if self.mode == MODE_PLACE:
            self.place_rotation = (self.place_rotation + 90) % 360
            # Update ghost immediately with new rotation
            if self.renderer.ghost:
                comp_def, row, col, _, _ = self.renderer.ghost
                valid = self.board.can_place(comp_def, row, col, self.place_rotation)
                self.renderer.ghost = (comp_def, row, col, self.place_rotation, valid)
            self._update_status(f"Rotation: {self.place_rotation}")
            self.renderer.redraw()
        elif self.mode == MODE_SELECT and self.selected_comp_id:
            cmd = RotateCmd(self, self.selected_comp_id)
            if self._execute_cmd(cmd):
                self._update_status(f"Rotated {self.selected_comp_id}")
            else:
                self._update_status("Cannot rotate - collision")
            self.renderer.redraw()

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
        if self.selected_comp_id:
            cmd = DeleteCmd(self, self.selected_comp_id)
            if self._execute_cmd(cmd):
                self._update_status(f"Deleted {self.selected_comp_id}")
                self.selected_comp_id = None
                self.renderer.selected_id = None
                self.renderer.redraw()

    def _move_selected(self, dr, dc):
        """Move selected component or division by (dr, dc) using arrow keys."""
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
        # Move component
        if not self.selected_comp_id:
            return
        pc = self.board.components.get(self.selected_comp_id)
        if not pc:
            return
        new_row = pc.anchor_row + dr
        new_col = pc.anchor_col + dc
        if self.board.can_place(pc.comp_def, new_row, new_col, pc.rotation, exclude_id=pc.id):
            cmd = MoveCmd(self, pc.id, pc.anchor_row, pc.anchor_col, new_row, new_col)
            self._execute_cmd(cmd)
            self.renderer.redraw()

    # ── Clipboard (Cut/Copy/Paste) ──

    def _copy_selected(self):
        """Copy selected component to system clipboard."""
        if not self.selected_comp_id:
            return
        pc = self.board.components.get(self.selected_comp_id)
        if not pc:
            return
        data = pc.to_dict()
        clip_text = CLIPBOARD_PREFIX + json.dumps(data)
        self.root.clipboard_clear()
        self.root.clipboard_append(clip_text)
        label = pc.label or pc.id
        self._update_status(f"Copied {pc.id} ({label})")

    def _cut_selected(self):
        """Cut selected component: copy to clipboard + delete."""
        if not self.selected_comp_id:
            return
        pc = self.board.components.get(self.selected_comp_id)
        if not pc:
            return
        # Copy to clipboard first
        data = pc.to_dict()
        clip_text = CLIPBOARD_PREFIX + json.dumps(data)
        self.root.clipboard_clear()
        self.root.clipboard_append(clip_text)
        label = pc.label or pc.id
        # Delete via command (supports undo)
        cmd = DeleteCmd(self, self.selected_comp_id)
        self._execute_cmd(cmd)
        self.selected_comp_id = None
        self.renderer.selected_id = None
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
            self.selected_comp_id = None
            self.renderer.selected_id = None
            self.renderer.ghost = None
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
            self.selected_comp_id = None
            self.renderer.selected_id = None
            self.renderer.ghost = None
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
        self.selected_comp_id = None
        self.renderer.selected_id = None
        self.renderer.ghost = None
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
        self.selected_comp_id = None
        self.renderer.selected_id = None
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
        if text:
            self.statusbar.config(text=text)
        else:
            mode_text = {'SELECT': 'Select mode', 'PLACE': 'Place mode',
                         'WIRE': 'Wire mode - click start pad',
                         'DIVIDE': 'Divide mode - click start edge',
                         'DELETE': 'Delete mode'}
            self.statusbar.config(text=mode_text.get(self.mode, 'Ready'))

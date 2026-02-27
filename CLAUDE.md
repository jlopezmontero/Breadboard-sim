# BreadboardSim - Perfboard Layout Simulator

## Overview

Tkinter-based perfboard visualization tool for designing component layouts on prototyping boards. Saves/loads `.bbsim` (JSON) files. No external dependencies beyond Python 3.8+ with tkinter (PIL optional for PNG export).

## Architecture

```
main.py              Entry point, single-instance lock check, Tk setup
gui.py               Main window: palette, toolbar, canvas, statusbar, all interaction
board.py             Data model: pads grid, components, guides, divisions (no GUI)
renderer.py          Canvas rendering: zoom/pan, pads, components, guides, labels
components.py        Component geometry definitions (DIP, axial, radial, QFP, PLCC, etc.)
component_library.py Category management, JSON component loading
persistence.py       Save/Load .bbsim JSON files
file_lock.py         Single-instance-per-file enforcement (Windows, ctypes)
library/             JSON component definitions (modules, semiconductors, etc.)
BreadboardSim.spec   PyInstaller spec (onefile, console=False, includes library/)
```

## Key Concepts

### Coordinate System
- **Grid coordinates** `(row, col)`: pad centers, integer. Row 0 = top, col 0 = left.
- **Edge coordinates** `(row, col)`: grid intersection points (corners between pads). Used for division lines.
- **Canvas coordinates** `(x, y)`: pixel positions on tkinter Canvas.
- `renderer.grid_to_canvas()` / `canvas_to_grid()` convert between them.
- Float variants (`canvas_to_grid_float`, `canvas_to_edge_float`) exist for sub-pad precision (proximity detection).

### Component Geometry
- `ComponentDef`: defines a component type. `pins` = list of `(row_offset, col_offset)`. `body_cells` = visual-only cells (don't block placement).
- `PlacedComponent`: instance on board. Has `anchor_row/col`, `rotation` (0/90/180/270), optional `label`.
- Pin 1 is always at anchor `(0,0)`. Rotation via `_rotate_offset()`.
- **Only pins block placement**. Body cells allow overlap (max 2 components per cell).

### Rendering Order
1. Board background
2. Grid lines
3. Division lines (green, between pads)
4. Pads (silver/gold)
5. Guide lines (wires, colored)
6. Components (Pass 1: all bodies stippled, Pass 2: inner bodies opaque + all pins/labels)
7. **Hidden guides** (stippled overlay for wires under component bodies)
8. Ghost preview (component being placed)
9. Row/col labels

### Command Pattern (Undo/Redo)
All board mutations go through `Command` subclasses with `execute()`/`undo()`:
- `PlaceCmd`, `DeleteCmd`, `MoveCmd`, `RotateCmd`, `RenameLabelCmd`
- `AddGuideCmd`, `DeleteGuideCmd` (splits at junctions/occupied pads)
- `AddDivisionCmd`, `DeleteDivisionCmd`, `MoveDivisionCmd`

### Interaction Modes
`MODE_SELECT` | `MODE_PLACE` | `MODE_WIRE` | `MODE_DIVIDE` | `MODE_DELETE`

Keyboard shortcuts: `W`=Wire, `D`=Divide, `X`=Delete. Esc/right-click exits to Select.

### Wire Segment Deletion
`DeleteGuideCmd` finds junction points along a wire (endpoints of other guides + occupied pads on the line) and only deletes the sub-segment closest to the click point.

### Single Instance per File
`file_lock.py` uses `.lock` files with `{"pid": N, "hwnd": N}`. On launch with a file argument, checks lock before creating Tk. If locked by live process, activates that window via Win32 API (`GetAncestor` + `SetForegroundWindow` + `AttachThreadInput`) and exits. Stale locks (dead PID) are ignored.

## Build

```bash
cd C:\BaremetalMaster\proyectos\SBC-WALL\breadboard-sim
taskkill //F //IM BreadboardSim.exe   # close running instance first
pyinstaller BreadboardSim.spec --noconfirm
# Output: dist/BreadboardSim.exe (~14 MB)
start dist/BreadboardSim.exe
```

## Adding Components

### Built-in (components.py)
Use factory functions: `make_dip()`, `make_axial()`, `make_radial_2pin()`, `make_header()`, `make_qfp()`, `make_plcc_adapter()`, `make_sip()`, etc. Register with `_register()`.

### JSON (library/*.json)
```json
{
  "components": [{
    "type_id": "MOD-XXX",
    "name": "Display Name",
    "ref_prefix": "M",
    "pins": [[row,col], ...],
    "body_cells": [[row,col], ...],
    "color": "#hex",
    "pin_labels": {"0": "VCC", "1": "GND"}
  }]
}
```
Loaded automatically from `library/` dir. Categories assigned by `type_id` prefix:
- `DIP-*` -> DIP ICs
- `AXIAL-*`, `RADIAL-*`, `CAP-*`, `ELEC-*`, `HC49`, `SIP-*` -> Passives
- `LED*`, `TO-*`, `DIODE*` -> Semiconductors
- `MOD-*` -> Modules
- `HDR-*` -> Connectors
- Everything else -> Custom

## File Format (.bbsim)

```json
{
  "version": 1,
  "board": {"rows": 57, "cols": 74, "title": "", "label_config": {}},
  "components": [{"id": "U1", "type": "DIP-40W", "anchor_row": 5, "anchor_col": 10, "rotation": 0, "label": "W65C02S"}],
  "guides": [[r1, c1, r2, c2, "#color"]],
  "divisions": [[r1, c1, r2, c2]]
}
```

## Platform

Windows-only for single-instance features (ctypes Win32 API). Core simulation works cross-platform.

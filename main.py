#!/usr/bin/env python3
"""
Breadboard Simulator - Perfboard visualization and component placement tool.
Part of the SBC-WALL project, but usable for any perfboard layout.

Usage:
    python main.py [file.bbsim]

Requirements: Python 3.8+ with tkinter (included in standard distribution).
No external dependencies.
"""

import sys
import os
import tkinter as tk
from file_lock import is_locked_by_other, activate_window, write_lock
from gui import BreadboardApp


def main():
    # If opening a specific file, check single-instance lock BEFORE creating Tk
    filepath = None
    if len(sys.argv) > 1:
        filepath = os.path.abspath(sys.argv[1])
        locked, hwnd = is_locked_by_other(filepath)
        if locked:
            activate_window(hwnd)
            sys.exit(0)

    root = tk.Tk()
    app = BreadboardApp(root)

    # Command line argument overrides recent file
    if filepath:
        try:
            from persistence import load_board
            app.board = load_board(filepath, app.library)
            app.renderer.board = app.board
            app._apply_label_config()
            app.filepath = filepath
            app.modified = False
            app._save_recent(filepath)
            app._update_title()
            # Acquire lock with the real HWND
            hwnd = int(root.wm_frame(), 16) if root.wm_frame() else root.winfo_id()
            write_lock(filepath, hwnd)
            app._file_lock_path = filepath
        except Exception as e:
            print(f"Error loading {filepath}: {e}", file=sys.stderr)

    root.mainloop()


if __name__ == '__main__':
    main()

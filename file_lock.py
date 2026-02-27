"""
File locking for single-instance-per-file enforcement.

Lock file: <filepath>.lock containing {"pid": N, "hwnd": N}.
Stale detection via kernel32.OpenProcess.
Window activation via user32.SetForegroundWindow + ShowWindow.
"""

import json
import os
import ctypes


def lock_path(filepath):
    """Return the lock file path for a given .bbsim file."""
    return os.path.abspath(filepath) + '.lock'


def read_lock(filepath):
    """Read lock file, return {'pid': N, 'hwnd': N} or None."""
    lp = lock_path(filepath)
    try:
        with open(lp, 'r', encoding='utf-8') as f:
            return json.loads(f.read())
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_lock(filepath, hwnd):
    """Write lock file with current PID and given HWND."""
    lp = lock_path(filepath)
    data = {'pid': os.getpid(), 'hwnd': hwnd}
    try:
        with open(lp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except OSError:
        pass


def release_lock(filepath):
    """Remove lock file if it belongs to the current process."""
    if not filepath:
        return
    lp = lock_path(filepath)
    try:
        info = read_lock(filepath)
        if info and info.get('pid') == os.getpid():
            os.remove(lp)
    except OSError:
        pass


def _process_exists(pid):
    """Check if a process with given PID is alive (Windows-only)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def is_locked_by_other(filepath):
    """Check if file is locked by another living process.

    Returns (True, hwnd) if locked by another process, (False, None) if free.
    """
    info = read_lock(filepath)
    if not info:
        return False, None
    pid = info.get('pid')
    hwnd = info.get('hwnd')
    if pid is None or hwnd is None:
        return False, None
    # Same process: not "other"
    if pid == os.getpid():
        return False, None
    # Check if that process is still alive
    if _process_exists(pid):
        return True, hwnd
    # Stale lock
    return False, None


def activate_window(hwnd):
    """Bring an existing window to the foreground (Windows-only).

    Resolves the true top-level owner window (Tkinter HWNDs may be children),
    restores from minimized via WM_SYSCOMMAND, and uses AttachThreadInput
    to bypass SetForegroundWindow restrictions.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Resolve to the real top-level owner (Tk winfo_id/wm_frame may be a child)
    GA_ROOTOWNER = 3
    top = user32.GetAncestor(hwnd, GA_ROOTOWNER)
    if top:
        hwnd = top

    # Restore from minimized using WM_SYSCOMMAND (works cross-process)
    if user32.IsIconic(hwnd):
        WM_SYSCOMMAND = 0x0112
        SC_RESTORE = 0xF120
        user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)

    # Attach to foreground thread to gain SetForegroundWindow permission
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
    our_tid = kernel32.GetCurrentThreadId()

    if fg_tid != our_tid:
        user32.AttachThreadInput(our_tid, fg_tid, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if fg_tid != our_tid:
        user32.AttachThreadInput(our_tid, fg_tid, False)

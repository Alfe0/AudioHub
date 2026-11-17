import os
import re
import sys
import copy
import glob
import time
import json
import math
import queue
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import shutil
import threading
import xml.etree.ElementTree as ET
import tkinter as tk
from ctypes import POINTER, c_wchar_p, c_int, c_uint, c_longlong, c_void_p, byref, wintypes

import pystray
import win32api
import win32con
import win32gui
import win32ui
from PIL import Image, ImageDraw, ImageTk

import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT, CoCreateInstance, CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume, IAudioEndpointVolume

APP_VERSION = "v0.9.0 Beta"

# Pillow compatibility fallback
try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS

# Enable High-DPI crisp rendering on Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# =====================================================================
# AppData Directory, Rotating Logger & Exception Hooks
# =====================================================================
APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AudioHub")
os.makedirs(APPDATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(APPDATA_DIR, "audiohub_config.json")
LOG_FILE = os.path.join(APPDATA_DIR, "audiohub.log")

LEGACY_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
LEGACY_CONFIG = os.path.join(LEGACY_DIR, "audiohub_config.json")
ICON_FILE = os.path.join(LEGACY_DIR, "audiohub.ico")

if os.path.exists(LEGACY_CONFIG) and not os.path.exists(CONFIG_FILE):
    try:
        shutil.copy2(LEGACY_CONFIG, CONFIG_FILE)
    except Exception:
        pass

log_handler = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(log_handler)


def _handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


def _handle_thread_exception(args):
    logging.error("Uncaught thread exception in %s", getattr(args.thread, "name", "unknown"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _handle_uncaught_exception
threading.excepthook = _handle_thread_exception

# =====================================================================
# Exact Windows Core Audio IPolicyConfig COM VTable Definition
# =====================================================================
CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
IID_IPolicyConfig = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")


class IPolicyConfig(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IPolicyConfig
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetMixFormat',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['out'], POINTER(c_void_p), 'ppFormat')),
        COMMETHOD([], HRESULT, 'GetDeviceFormat',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_int, 'bDefault'),
                  (['out'], POINTER(c_void_p), 'ppFormat')),
        COMMETHOD([], HRESULT, 'ResetDeviceFormat',
                  (['in'], c_wchar_p, 'pszDeviceName')),
        COMMETHOD([], HRESULT, 'SetDeviceFormat',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_void_p, 'pEndpointFormat'),
                  (['in'], c_void_p, 'pMixFormat')),
        COMMETHOD([], HRESULT, 'GetProcessingPeriod',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_int, 'bDefault'),
                  (['out'], POINTER(c_longlong), 'pmftDefaultPeriod'),
                  (['out'], POINTER(c_longlong), 'pmftMinimumPeriod')),
        COMMETHOD([], HRESULT, 'SetProcessingPeriod',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], POINTER(c_longlong), 'pmftPeriod')),
        COMMETHOD([], HRESULT, 'GetShareMode',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['out'], POINTER(c_void_p), 'pMode')),
        COMMETHOD([], HRESULT, 'SetShareMode',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_void_p, 'pMode')),
        COMMETHOD([], HRESULT, 'GetPropertyValue',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_int, 'bFxStore'),
                  (['in'], c_void_p, 'pKey'),
                  (['out'], POINTER(c_void_p), 'pv')),
        COMMETHOD([], HRESULT, 'SetPropertyValue',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_int, 'bFxStore'),
                  (['in'], c_void_p, 'pKey'),
                  (['in'], c_void_p, 'pv')),
        COMMETHOD([], HRESULT, 'SetDefaultEndpoint',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_uint, 'role')),
        COMMETHOD([], HRESULT, 'SetEndpointVisibility',
                  (['in'], c_wchar_p, 'pszDeviceName'),
                  (['in'], c_int, 'bVisible')),
    ]


# =====================================================================
# Theme & Configuration
# =====================================================================
DEFAULT_CONFIG = {
    "hotkey_mixer": "alt+q",
    "hotkey_switch": "alt+w",
    "show_system_sounds": True,
    "device_icons": {},
    "excluded_devices": []
}

DEVICE_ICON_TYPES = ["speaker", "headphones", "monitor"]

BLOCKED_HOTKEYS = {
    "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+s", "ctrl+w",
    "ctrl+t", "ctrl+n", "ctrl+f", "ctrl+p", "ctrl+r", "ctrl+shift+esc", "ctrl+alt+delete",
    "alt+f4", "alt+tab", "alt+space", "alt+enter", "alt+escape",
    "win+l", "win+d", "win+e", "win+r", "win+i", "win+x", "win+tab", "win+space"
}

THEME = {
    "header_bg": "#323546",
    "dev_btn_bg": "#262936",
    "dev_btn_hover": "#3E4257",
    "dropdown_bg": "#1E202B",
    "dropdown_active": "#3B4056",
    "settings_bg": "#20222E",
    "card_bg": "#2A2D3D",
    "body_bg": "#181920",
    "menu_bg": "#1A1C26",
    "menu_hover": "#2E3244",
    "border": "#45495E",
    "text_main": "#FFFFFF",
    "text_dim": "#A6ADC8",
    "accent": "#9499B3",
    "accent_bright": "#38BDF8",
    "badge_bg": "#1E3A5F",
    "track": "#3B3E4D",
    "mute": "#FF4D4D",
    "osd_bg": "#14151C"
}

# Comprehensive Virtual Key Mapping (Supports Single Keys & Tkinter Keysyms)
VK_MAP = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "f13": 0x7C, "f14": 0x7D, "f15": 0x7E, "f16": 0x7F, "f17": 0x80, "f18": 0x81,
    "f19": 0x82, "f20": 0x83, "f21": 0x84, "f22": 0x85, "f23": 0x86, "f24": 0x87,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "escape": 0x1B,
    "backspace": 0x08, "back": 0x08, "caps_lock": 0x14, "capital": 0x14,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "page_up": 0x21, "prior": 0x21, "pgup": 0x21,
    "page_down": 0x22, "next": 0x22, "pgdn": 0x22,
    "pause": 0x13, "scroll_lock": 0x91, "num_lock": 0x90, "print": 0x2C, "snapshot": 0x2C,
    "kp_0": 0x60, "kp_1": 0x61, "kp_2": 0x62, "kp_3": 0x63, "kp_4": 0x64,
    "kp_5": 0x65, "kp_6": 0x66, "kp_7": 0x67, "kp_8": 0x68, "kp_9": 0x69,
    "kp_multiply": 0x6A, "asterisk": 0x6A, "kp_add": 0x6B, "plus": 0xBB,
    "kp_subtract": 0x6D, "kp_decimal": 0x6E, "kp_divide": 0x6F,
    "backslash": 0xDC, "slash": 0xBF, "comma": 0xBC, "period": 0xBE,
    "semicolon": 0xBA, "quote": 0xDE, "apostrophe": 0xDE,
    "bracketleft": 0xDB, "bracketright": 0xDD,
    "minus": 0xBD, "equal": 0xBB, "grave": 0xC0, "asciitilde": 0xC0, "quoteleft": 0xC0
}

WM_APP_RELOAD_HOTKEYS = 0x8001
WM_APP_PAUSE_HOTKEYS = 0x8002
WM_APP_QUIT_HOTKEYS = 0x8003


# =====================================================================
# Brand Identity: Master Logo & Simplified High-Contrast Tray Icon
# =====================================================================
def create_brand_logo_pil(size=256, simplified_tray=False):
    """
    Generates AudioHub's visual identity using 4x supersampling:
    - Master Logo (simplified_tray=False): Deep indigo squircle badge with glowing cyan ring,
      crisp white speaker cone, and 3 equalizer soundwave bars.
    - Tray Icon (simplified_tray=True): Borderless transparent canvas with oversized high-contrast
      speaker cone + cyan equalizer bars filling 92% of the icon for maximum clarity at 16x16px.
    """
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if not simplified_tray:
        # Outer squircle badge
        pad = int(s * 0.05)
        radius = int(s * 0.24)
        d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill="#161925", outline="#38BDF8", width=max(4, int(s * 0.035)))
        # Subtle inner glow ring
        in_pad = int(s * 0.09)
        d.rounded_rectangle([in_pad, in_pad, s - in_pad, s - in_pad], radius=int(radius * 0.82), outline="#252B42", width=max(2, int(s * 0.015)))

        # Speaker Cone inside badge
        cone = [
            (int(s * 0.22), int(s * 0.40)),
            (int(s * 0.33), int(s * 0.40)),
            (int(s * 0.48), int(s * 0.24)),
            (int(s * 0.48), int(s * 0.76)),
            (int(s * 0.33), int(s * 0.60)),
            (int(s * 0.22), int(s * 0.60)),
        ]
        d.polygon(cone, fill="#FFFFFF")

        # 3 Equalizer Pulse Bars on the right (AudioHub signature)
        bar_w = max(6, int(s * 0.065))
        r_bar = bar_w // 2
        # Bar 1 (Inner - Cyan)
        x1 = int(s * 0.56)
        d.rounded_rectangle([x1, int(s * 0.36), x1 + bar_w, int(s * 0.64)], radius=r_bar, fill="#38BDF8")
        # Bar 2 (Middle Tall - Bright Cyan)
        x2 = int(s * 0.67)
        d.rounded_rectangle([x2, int(s * 0.25), x2 + bar_w, int(s * 0.75)], radius=r_bar, fill="#7DD3FC")
        # Bar 3 (Outer - Indigo/Cyan)
        x3 = int(s * 0.78)
        d.rounded_rectangle([x3, int(s * 0.41), x3 + bar_w, int(s * 0.59)], radius=r_bar, fill="#38BDF8")
    else:
        # Simplified, borderless, high-contrast symbol for tiny Windows System Tray (16x16 / 24x24)
        cone = [
            (int(s * 0.06), int(s * 0.34)),
            (int(s * 0.24), int(s * 0.34)),
            (int(s * 0.46), int(s * 0.12)),
            (int(s * 0.46), int(s * 0.88)),
            (int(s * 0.24), int(s * 0.66)),
            (int(s * 0.06), int(s * 0.66)),
        ]
        d.polygon(cone, fill="#FFFFFF")

        bar_w = max(8, int(s * 0.11))
        r_bar = bar_w // 2
        # Bold 2-bar equalizer pulse for ultra-crisp tray readability
        x1 = int(s * 0.58)
        d.rounded_rectangle([x1, int(s * 0.28), x1 + bar_w, int(s * 0.72)], radius=r_bar, fill="#38BDF8")
        x2 = int(s * 0.78)
        d.rounded_rectangle([x2, int(s * 0.14), x2 + bar_w, int(s * 0.86)], radius=r_bar, fill="#7DD3FC")

    return img.resize((size, size), RESAMPLE_LANCZOS)


def ensure_app_ico_file():
    """Exports audiohub.ico next to the script so PyInstaller or shortcuts can use the official logo."""
    if not getattr(sys, "frozen", False) and not os.path.exists(ICON_FILE):
        try:
            master = create_brand_logo_pil(size=256, simplified_tray=False)
            master.save(ICON_FILE, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        except Exception:
            logging.exception("Failed to generate audiohub.ico")


# =====================================================================
# Custom Tray Icon Subclass (Intercepts Left & Right Clicks Cleanly)
# =====================================================================
class AudioHubTrayIcon(pystray.Icon):
    def __init__(self, app_ref, *args, **kwargs):
        self.app_ref = app_ref
        super().__init__(*args, **kwargs)

    def _on_notify(self, wparam, lparam):
        if lparam == win32con.WM_LBUTTONUP:
            self.app_ref.post(self.app_ref.toggle_flyout, True)
        elif lparam == win32con.WM_RBUTTONUP:
            self.app_ref.post(self.app_ref.show_custom_tray_menu)
        else:
            super()._on_notify(wparam, lparam)


# =====================================================================
# Custom Smooth Slider Widget
# =====================================================================
class ModernSlider(tk.Canvas):
    def __init__(self, parent, app_ref, value=100, muted=False, bg_color="#181920", accent_color="#9499B3", command=None, width=320, height=40):
        super().__init__(parent, width=width, height=height, bg=bg_color, highlightthickness=0, bd=0, cursor="hand2")
        self.app_ref = app_ref
        self.value = max(0, min(100, int(value)))
        self.muted = bool(muted)
        self.bg_color = bg_color
        self.accent_color = accent_color
        self.command = command
        self.w = width
        self.h = height
        self.pad_x = max(10, int(12 * app_ref.dpi_scale))

        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Configure>", self._on_resize)
        self.draw()

    def set_state(self, val, muted=False):
        new_v = max(0, min(100, int(val)))
        new_m = bool(muted)
        if new_v != self.value or new_m != self.muted:
            self.value = new_v
            self.muted = new_m
            self.draw()

    def _on_resize(self, event):
        if event.width > 50 and event.width != self.w:
            self.w = event.width
            self.draw()

    def _on_press(self, event):
        self.app_ref.is_dragging_slider = True
        self._update_from_x(event.x)

    def _on_drag(self, event):
        self.app_ref.is_dragging_slider = True
        self._update_from_x(event.x)

    def _on_release(self, event):
        self.app_ref.is_dragging_slider = False

    def _update_from_x(self, x_coord):
        usable_w = max(1, self.w - (2 * self.pad_x))
        rel_x = max(0, min(usable_w, x_coord - self.pad_x))
        new_val = int(round((rel_x / usable_w) * 100))
        if new_val != self.value or self.muted:
            self.value = new_val
            self.muted = (new_val == 0)
            self.draw()
            if self.command:
                self.command(self.value)

    def _on_wheel(self, event):
        step = 2 if event.delta > 0 else -2
        new_val = max(0, min(100, self.value + step))
        if new_val != self.value or self.muted:
            self.value = new_val
            self.muted = (new_val == 0)
            self.draw()
            if self.command:
                self.command(self.value)
        return "break"

    def draw(self):
        self.delete("all")
        cy = self.h // 2
        usable_w = max(1, self.w - (2 * self.pad_x))
        thumb_x = self.pad_x + int((self.value / 100.0) * usable_w)

        is_silent = self.muted or (self.value == 0)
        active_col = THEME["mute"] if is_silent else self.accent_color
        track_w = max(2, int(3 * self.app_ref.dpi_scale))

        self.create_line(self.pad_x, cy, self.w - self.pad_x, cy, fill=THEME["track"], width=track_w)
        if thumb_x > self.pad_x:
            self.create_line(self.pad_x, cy, thumb_x, cy, fill=active_col, width=track_w)

        tw = max(4, int(5 * self.app_ref.dpi_scale))
        th = max(10, int(13 * self.app_ref.dpi_scale))
        self.create_oval(thumb_x - tw, cy - th, thumb_x + tw, cy - th + (tw * 2), fill=active_col, outline="")
        self.create_oval(thumb_x - tw, cy + th - (tw * 2), thumb_x + tw, cy + th, fill=active_col, outline="")
        self.create_rectangle(thumb_x - tw, cy - th + tw, thumb_x + tw + 1, cy + th - tw, fill=active_col, outline="")


# =====================================================================
# Main Application
# =====================================================================
class AudioHubFlyout:
    def __init__(self):
        comtypes.CoInitialize()
        ensure_app_ico_file()
        self.config = self.load_config()
        self.icon_cache = {}
        self.vector_cache = {}
        self.devices = []
        self.current_device_id = None
        self._master_vol = None

        self.is_visible = False
        self.had_focus = False
        self._quitting = False
        self._cached_hwnd = 0
        self._target_monitor_work = None
        self._last_topology_sig = None
        self._sync_tick_count = 0

        self.device_list_expanded = False
        self.settings_expanded = False
        self.about_expanded = False
        self.is_dragging_slider = False
        self.recording_target = None
        self.was_mouse_down = False
        self.open_timestamp = 0.0
        self.last_hide_ts = 0.0

        self.master_sync_ref = None
        self.app_sync_refs = []
        self.poll_after_id = None
        self.sync_after_id = None
        self.body_canvas = None
        self.apps_frame = None
        self.tray_menu_win = None
        self.about_win = None

        # Root Flyout Window
        self.root = tk.Tk()
        self.root.report_callback_exception = self._report_tk_exception
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=THEME["border"])

        try:
            raw_dpi = self.root.winfo_fpixels("1i")
            self.dpi_scale = max(0.85, min(2.0, raw_dpi / 120.0))
        except Exception:
            self.dpi_scale = 1.0

        self.flyout_width = int(480 * self.dpi_scale)
        self.icon_size = int(36 * self.dpi_scale)
        self.mute_icon_size = int(30 * self.dpi_scale)
        self.slider_height = int(40 * self.dpi_scale)

        self.apply_win11_rounded_corners(self.root)

        self.main_frame = tk.Frame(self.root, bg=THEME["body_bg"])
        self.main_frame.pack(fill="both", expand=True, padx=1, pady=1)

        self.osd_win = None
        self.osd_after_id = None

        # Thread-safe UI Queue
        self.ui_queue = queue.Queue()
        self.process_ui_queue()

        # Pre-render core vector icons & Brand Logos
        self.logo_large_tk = ImageTk.PhotoImage(create_brand_logo_pil(size=int(64 * self.dpi_scale), simplified_tray=False))
        self.logo_menu_tk = ImageTk.PhotoImage(create_brand_logo_pil(size=int(26 * self.dpi_scale), simplified_tray=False))

        self.default_app_icon = self.draw_system_audio_icon(THEME["body_bg"], size=self.icon_size)
        self.system_sounds_icon = self.draw_system_audio_icon(THEME["body_bg"], size=self.icon_size)
        self.whatsapp_icon = self.draw_whatsapp_icon(THEME["body_bg"], size=self.icon_size)
        self.speaker_header_icon = self.draw_vector_speaker(THEME["header_bg"], "#FFFFFF", muted=False, size=self.icon_size)
        self.header_red_mute_small = self.draw_vector_speaker(THEME["header_bg"], THEME["mute"], muted=True, size=self.mute_icon_size)
        self.app_red_mute_icon = self.draw_vector_speaker(THEME["body_bg"], THEME["mute"], muted=True, size=self.mute_icon_size)

        gear_sz = int(28 * self.dpi_scale)
        self.gear_icon_normal = self.draw_gear_icon(THEME["dev_btn_bg"], THEME["text_dim"], size=gear_sz)
        self.gear_icon_hover = self.draw_gear_icon(THEME["dev_btn_hover"], "#FFFFFF", size=gear_sz)
        self.gear_icon_active = self.draw_gear_icon(THEME["dropdown_active"], THEME["accent_bright"], size=gear_sz)

        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<KeyPress>", self._on_key_record_event)

        # Start Zero-CPU Blocking Win32 Hotkey Thread
        self.hk_thread_id = None
        self.hk_ready_event = threading.Event()
        self.hk_thread = threading.Thread(target=self._win32_hotkey_loop, name="HotkeyThread", daemon=True)
        self.hk_thread.start()
        self.hk_ready_event.wait(timeout=1.0)
        self.reload_hotkeys()

        self.start_tray_icon()

    def _report_tk_exception(self, exc, val, tb):
        logging.error("Tkinter callback exception", exc_info=(exc, val, tb))

    # -----------------------------------------------------------------
    # Thread-Safe UI Dispatcher & Multi-Monitor Helper
    # -----------------------------------------------------------------
    def post(self, fn, *args):
        if not self._quitting:
            self.ui_queue.put((fn, args))

    def process_ui_queue(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    logging.exception("UI queue task failed")
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(50, self.process_ui_queue)

    def get_active_monitor_work_area(self):
        try:
            cx, cy = win32api.GetCursorPos()
            monitor = win32api.MonitorFromPoint((cx, cy), win32con.MONITOR_DEFAULTTONEAREST)
            return win32api.GetMonitorInfo(monitor)["Work"]
        except Exception:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            return (0, 0, sw, sh - 48)

    def get_hwnd(self, window):
        try:
            window.update_idletasks()
            return user32.GetParent(window.winfo_id()) or window.winfo_id()
        except Exception:
            return window.winfo_id()

    def apply_win11_rounded_corners(self, window):
        try:
            hwnd = self.get_hwnd(window)
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = c_int(2)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, byref(DWMWCP_ROUND), ctypes.sizeof(DWMWCP_ROUND)
            )
        except Exception:
            logging.exception("Failed to set rounded corners")

    def apply_noactivate_style(self, window):
        try:
            hwnd = self.get_hwnd(window)
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            logging.exception("Failed to apply noactivate style")

    # -----------------------------------------------------------------
    # On-Demand Polling & Live Session/Device Auto-Discovery
    # -----------------------------------------------------------------
    def start_active_watchers(self):
        self.stop_active_watchers()
        self.was_mouse_down = False
        self._sync_tick_count = 0
        self._poll_outside_and_focus()
        self._live_audio_sync_step()

    def stop_active_watchers(self):
        if self.poll_after_id:
            try:
                self.root.after_cancel(self.poll_after_id)
            except Exception:
                pass
            self.poll_after_id = None
        if self.sync_after_id:
            try:
                self.root.after_cancel(self.sync_after_id)
            except Exception:
                pass
            self.sync_after_id = None

    def _poll_outside_and_focus(self):
        if not self.is_visible or self._quitting:
            return

        l_down = (win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000) != 0
        r_down = (win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000) != 0
        is_down = l_down or r_down

        if not is_down and self.is_dragging_slider:
            self.is_dragging_slider = False

        just_pressed = is_down and not self.was_mouse_down
        self.was_mouse_down = is_down

        elapsed = time.time() - self.open_timestamp
        if elapsed > 0.25 and not self.is_dragging_slider and self.recording_target is None:
            if just_pressed:
                try:
                    cx, cy = win32api.GetCursorPos()
                    wx = self.root.winfo_rootx()
                    wy = self.root.winfo_rooty()
                    ww = self.root.winfo_width()
                    wh = self.root.winfo_height()
                    if not (wx <= cx <= wx + ww and wy <= cy <= wy + wh):
                        self.hide_flyout()
                        return
                except Exception:
                    pass

            if elapsed > 0.45:
                try:
                    fg = user32.GetForegroundWindow()
                    mine = fg in (self._cached_hwnd, self.root.winfo_id())
                    if mine:
                        self.had_focus = True
                    elif self.had_focus and fg != 0:
                        self.hide_flyout()
                        return
                except Exception:
                    pass

        self.poll_after_id = self.root.after(30, self._poll_outside_and_focus)

    def _compute_audio_topology_signature(self):
        try:
            speakers = AudioUtilities.GetSpeakers()
            cur_dev_id = getattr(speakers, "id", None) or (speakers.GetId() if hasattr(speakers, "GetId") else None)
        except Exception:
            cur_dev_id = None

        dev_ids = []
        try:
            for dev in AudioUtilities.GetAllDevices():
                state_val = getattr(dev.state, "value", dev.state)
                if state_val == 1 and dev.id.startswith("{0.0.0."):
                    dev_ids.append(dev.id)
        except Exception:
            pass

        active_pids = []
        try:
            for s in AudioUtilities.GetAllSessions():
                pid = getattr(s, "ProcessId", None)
                if s.Process is not None and pid:
                    try:
                        if s.Process.is_running():
                            active_pids.append(pid)
                    except Exception:
                        pass
                elif pid == 0:
                    active_pids.append(0)
        except Exception:
            pass

        return (cur_dev_id, tuple(dev_ids), tuple(sorted(set(active_pids))))

    def _live_audio_sync_step(self):
        if not self.is_visible or self._quitting:
            return

        self._sync_tick_count += 1

        if not self.is_dragging_slider and self.recording_target is None:
            if self._sync_tick_count % 6 == 0:
                new_sig = self._compute_audio_topology_signature()
                if self._last_topology_sig is not None and new_sig != self._last_topology_sig:
                    self._master_vol = None
                    self.build_flyout_ui()
                    self.position_flyout()
                    self.sync_after_id = self.root.after(200, self._live_audio_sync_step)
                    return

            if self.master_sync_ref:
                try:
                    m_vol = self.get_master_volume_interface()
                    if m_vol:
                        cur_v = int(round(m_vol.GetMasterVolumeLevelScalar() * 100))
                        cur_m = bool(m_vol.GetMute())
                        self.master_sync_ref["update_ui"](cur_m, cur_v)
                except Exception:
                    self._master_vol = None

            for item in list(self.app_sync_refs):
                try:
                    sessions = item["sessions"]
                    if sessions:
                        ctl = sessions[0]._ctl.QueryInterface(ISimpleAudioVolume)
                        cur_v = int(round(ctl.GetMasterVolume() * 100))
                        cur_m = bool(ctl.GetMute())
                        item["update_ui"](cur_m, cur_v)
                except Exception:
                    pass

        self.sync_after_id = self.root.after(200, self._live_audio_sync_step)

    # -----------------------------------------------------------------
    # Zero-CPU Native Win32 Hotkey Engine (Supports Single OR Combo Keys!)
    # -----------------------------------------------------------------
    def parse_hotkey_string(self, hk_str):
        if not hk_str or not isinstance(hk_str, str):
            return None
        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        MOD_SHIFT = 0x0004
        MOD_WIN = 0x0008
        MOD_NOREPEAT = 0x4000

        mods = MOD_NOREPEAT
        vk = 0
        parts = [p.strip().lower() for p in hk_str.split("+") if p.strip()]
        for p in parts:
            if p in ("alt", "menu"):
                mods |= MOD_ALT
            elif p in ("ctrl", "control"):
                mods |= MOD_CONTROL
            elif p == "shift":
                mods |= MOD_SHIFT
            elif p in ("win", "windows", "super"):
                mods |= MOD_WIN
            elif p in VK_MAP:
                vk = VK_MAP[p]
            elif len(p) == 1 and p.isalnum() and p.isascii():
                vk = ord(p.upper())
            else:
                return None

        return (mods, vk) if vk != 0 else None

    def reload_hotkeys(self):
        if self.hk_thread_id:
            user32.PostThreadMessageW(self.hk_thread_id, WM_APP_RELOAD_HOTKEYS, 0, 0)

    def pause_hotkeys(self):
        if self.hk_thread_id:
            user32.PostThreadMessageW(self.hk_thread_id, WM_APP_PAUSE_HOTKEYS, 0, 0)

    def _register_single_hotkey(self, hk_id, hk_str, label):
        parsed = self.parse_hotkey_string(hk_str)
        if not parsed:
            logging.warning("Invalid hotkey format for %s: %s", label, hk_str)
            return
        ctypes.set_last_error(0)
        res = user32.RegisterHotKey(None, hk_id, parsed[0], parsed[1])
        if not res:
            err = ctypes.get_last_error()
            logging.warning("RegisterHotKey failed for %s (%s), WinError=%d", label, hk_str, err)
            msg = f"Shortcut '{hk_str.upper()}' is in use by another app!" if err == 1409 else f"Cannot bind '{hk_str.upper()}'"
            self.post(self.show_top_left_osd, "Shortcut Conflict", msg)

    def _win32_hotkey_loop(self):
        self.hk_thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(byref(msg), None, 0, 0, 0)
        self.hk_ready_event.set()

        WM_HOTKEY = 0x0312
        while True:
            ret = user32.GetMessageW(byref(msg), None, 0, 0)
            if ret <= 0:
                break

            if msg.message == WM_HOTKEY:
                if msg.wParam == 1:
                    self.post(self.toggle_flyout)
                elif msg.wParam == 2:
                    self.post(self.cycle_audio_device, True)
            elif msg.message == WM_APP_RELOAD_HOTKEYS:
                user32.UnregisterHotKey(None, 1)
                user32.UnregisterHotKey(None, 2)
                self._register_single_hotkey(1, self.config.get("hotkey_mixer", "alt+q"), "Mixer")
                self._register_single_hotkey(2, self.config.get("hotkey_switch", "alt+w"), "Switch")
            elif msg.message == WM_APP_PAUSE_HOTKEYS:
                user32.UnregisterHotKey(None, 1)
                user32.UnregisterHotKey(None, 2)
            elif msg.message == WM_APP_QUIT_HOTKEYS:
                user32.UnregisterHotKey(None, 1)
                user32.UnregisterHotKey(None, 2)
                break

    # -----------------------------------------------------------------
    # Click-to-Record Shortcut Handler (Supports 1 Key or 2+ Keys!)
    # -----------------------------------------------------------------
    def start_recording_hotkey(self, target_key, btn_widget):
        self.recording_target = target_key
        self.pause_hotkeys()
        btn_widget.config(text="Press any key...", bg=THEME["accent_bright"], fg="#11111B")
        self.root.focus_force()

    def _on_key_record_event(self, event):
        if not self.recording_target:
            return

        keysym = event.keysym.lower()
        if keysym == "escape":
            self.recording_target = None
            self.reload_hotkeys()
            self.build_flyout_ui()
            return

        if keysym in ("alt_l", "alt_r", "control_l", "control_r", "shift_l", "shift_r", "win_l", "win_r", "super_l", "super_r"):
            return

        mods = []
        if (win32api.GetAsyncKeyState(win32con.VK_CONTROL) & 0x8000) != 0:
            mods.append("ctrl")
        if (win32api.GetAsyncKeyState(win32con.VK_MENU) & 0x8000) != 0:
            mods.append("alt")
        if (win32api.GetAsyncKeyState(win32con.VK_SHIFT) & 0x8000) != 0:
            mods.append("shift")
        if ((win32api.GetAsyncKeyState(win32con.VK_LWIN) & 0x8000) != 0 or
                (win32api.GetAsyncKeyState(win32con.VK_RWIN) & 0x8000) != 0):
            mods.append("win")

        key_name = keysym
        if key_name in VK_MAP:
            pass
        elif len(key_name) == 1 and key_name.isalnum() and key_name.isascii():
            pass
        elif 65 <= event.keycode <= 90 or 48 <= event.keycode <= 57:
            key_name = chr(event.keycode).lower()
        else:
            return

        combo = "+".join(mods + [key_name])

        if not self.parse_hotkey_string(combo):
            return

        if combo in BLOCKED_HOTKEYS:
            self.recording_target = None
            self.reload_hotkeys()
            self.build_flyout_ui()
            self.show_top_left_osd("Shortcut Conflict", f"'{combo.upper()}' is a reserved system shortcut!")
            return

        other_key = "hotkey_switch" if self.recording_target == "hotkey_mixer" else "hotkey_mixer"
        if combo == self.config.get(other_key, "").lower():
            self.recording_target = None
            self.reload_hotkeys()
            self.build_flyout_ui()
            self.show_top_left_osd("Shortcut Conflict", f"'{combo.upper()}' is already assigned to the other action!")
            return

        self.config[self.recording_target] = combo
        self.recording_target = None
        self.save_config()
        self.reload_hotkeys()
        self.build_flyout_ui()

    def _on_escape(self, event):
        if self.recording_target:
            self.recording_target = None
            self.reload_hotkeys()
            self.build_flyout_ui()
        else:
            self.hide_flyout()

    # -----------------------------------------------------------------
    # Config & Device Customization
    # -----------------------------------------------------------------
    def load_config(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        cfg.update(data)
            except Exception:
                logging.exception("Failed to read config")
        if not isinstance(cfg.get("device_icons"), dict):
            cfg["device_icons"] = {}
        if not isinstance(cfg.get("excluded_devices"), list):
            cfg["excluded_devices"] = []
        return cfg

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            logging.exception("Failed to save config")

    def get_device_icon_type(self, dev_id, dev_name=""):
        saved = self.config.get("device_icons", {}).get(dev_id)
        if saved in DEVICE_ICON_TYPES:
            return saved
        lower = dev_name.lower()
        if re.search(r"\b(m60|edifier|speaker|speakers|realtek|soundbar)\b", lower):
            return "speaker"
        if re.search(r"\b(nvidia|amd|lg|samsung|dell|asus|benq|acer|msi|monitor|hdmi|displayport|tv)\b", lower):
            return "monitor"
        return "headphones"

    def cycle_device_icon_type(self, dev_id, dev_name=""):
        curr = self.get_device_icon_type(dev_id, dev_name)
        nxt = DEVICE_ICON_TYPES[(DEVICE_ICON_TYPES.index(curr) + 1) % len(DEVICE_ICON_TYPES)]
        self.config.setdefault("device_icons", {})[dev_id] = nxt
        self.save_config()
        if self.is_visible:
            self.build_flyout_ui()
            self.position_flyout()

    def toggle_device_exclusion(self, dev_id):
        excluded = self.config.setdefault("excluded_devices", [])
        if dev_id in excluded:
            excluded.remove(dev_id)
        else:
            excluded.append(dev_id)
        self.save_config()
        if self.is_visible:
            self.build_flyout_ui()
            self.position_flyout()

    def toggle_show_system_sounds(self):
        curr = bool(self.config.get("show_system_sounds", True))
        self.config["show_system_sounds"] = not curr
        self.save_config()
        if self.is_visible:
            self.build_flyout_ui()
            self.position_flyout()

    # -----------------------------------------------------------------
    # 4x Supersampled Vector Icons & Safe GDI Extraction
    # -----------------------------------------------------------------
    def hex_to_rgba(self, hex_color):
        h = hex_color.lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)

    def draw_whatsapp_icon(self, bg_hex, size=36):
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), bg_hex)
        draw = ImageDraw.Draw(img)

        pad = int(s * 0.06)
        draw.ellipse([pad, pad, s - pad, s - pad], fill="#25D366")

        b_pad = int(s * 0.21)
        w_ring = max(6, int(s * 0.065))
        draw.ellipse([b_pad, b_pad, s - b_pad, s - b_pad], outline="#FFFFFF", width=w_ring)
        tail = [
            (int(s * 0.22), int(s * 0.78)),
            (int(s * 0.28), int(s * 0.62)),
            (int(s * 0.38), int(s * 0.72))
        ]
        draw.polygon(tail, fill="#FFFFFF")
        in_pad = b_pad + w_ring - 2
        draw.ellipse([in_pad, in_pad, s - in_pad, s - in_pad], fill="#25D366")

        w_phone = max(8, int(s * 0.085))
        draw.arc([int(s * 0.34), int(s * 0.32), int(s * 0.66), int(s * 0.64)], start=125, end=45, fill="#FFFFFF", width=w_phone)
        draw.ellipse([int(s * 0.35), int(s * 0.35), int(s * 0.46), int(s * 0.46)], fill="#FFFFFF")
        draw.ellipse([int(s * 0.54), int(s * 0.52), int(s * 0.65), int(s * 0.63)], fill="#FFFFFF")

        img = img.resize((size, size), RESAMPLE_LANCZOS)
        return ImageTk.PhotoImage(img)

    def extract_uwp_manifest_icon(self, exe_path, bg_hex, size=36):
        try:
            curr_dir = os.path.dirname(exe_path)
            manifest_path = None
            pkg_root = None
            for _ in range(4):
                candidate = os.path.join(curr_dir, "AppxManifest.xml")
                if os.path.exists(candidate):
                    manifest_path = candidate
                    pkg_root = curr_dir
                    break
                parent = os.path.dirname(curr_dir)
                if parent == curr_dir:
                    break
                curr_dir = parent

            if not manifest_path or not pkg_root:
                return None

            tree = ET.parse(manifest_path)
            root = tree.getroot()
            logo_rel_paths = []

            for elem in root.iter():
                for attr in ("Square44x44Logo", "Square150x150Logo", "Logo"):
                    if attr in elem.attrib:
                        logo_rel_paths.append(elem.attrib[attr])
                if elem.tag.endswith("Logo") and elem.text:
                    logo_rel_paths.append(elem.text.strip())

            candidates = []
            for rel in logo_rel_paths:
                full_base = os.path.join(pkg_root, rel.replace("/", "\\"))
                base_dir = os.path.dirname(full_base)
                stem = os.path.splitext(os.path.basename(full_base))[0]
                if os.path.exists(base_dir):
                    for f in glob.glob(os.path.join(base_dir, f"{stem}*.png")):
                        candidates.append(f)

            if not candidates:
                return None

            def score_png(path):
                name = os.path.basename(path).lower()
                sc = 0
                if "unplated" in name:
                    sc += 100
                if "256" in name or "scale-200" in name:
                    sc += 50
                elif "64" in name or "48" in name or "scale-150" in name:
                    sc += 30
                return sc

            candidates.sort(key=score_png, reverse=True)
            best_png = candidates[0]

            if "whatsapp" in exe_path.lower() and "unplated" not in os.path.basename(best_png).lower():
                return self.whatsapp_icon

            raw_img = Image.open(best_png).convert("RGBA")
            bg_img = Image.new("RGBA", raw_img.size, self.hex_to_rgba(bg_hex))
            comp = Image.alpha_composite(bg_img, raw_img).resize((size, size), RESAMPLE_LANCZOS)
            return ImageTk.PhotoImage(comp)
        except Exception:
            logging.exception("UWP icon extraction failed for %s", exe_path)
            return None

    def get_device_vector_icon(self, icon_type, bg_hex, fg_hex="#FFFFFF", size=26):
        key = f"dev:{icon_type}:{bg_hex}:{fg_hex}:{size}"
        if key in self.vector_cache:
            return self.vector_cache[key]

        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), bg_hex)
        draw = ImageDraw.Draw(img)
        w = max(5, int(s * 0.07))

        if icon_type == "speaker":
            draw.rounded_rectangle([int(s * 0.22), int(s * 0.10), int(s * 0.78), int(s * 0.90)], radius=14, outline=fg_hex, width=w)
            draw.ellipse([int(s * 0.43), int(s * 0.22), int(s * 0.57), int(s * 0.36)], fill=fg_hex)
            draw.ellipse([int(s * 0.34), int(s * 0.46), int(s * 0.66), int(s * 0.78)], outline=fg_hex, width=w)
        elif icon_type == "monitor":
            draw.rounded_rectangle([int(s * 0.12), int(s * 0.16), int(s * 0.88), int(s * 0.70)], radius=12, outline=fg_hex, width=w)
            draw.line([int(s * 0.50), int(s * 0.70), int(s * 0.50), int(s * 0.86)], fill=fg_hex, width=w)
            draw.line([int(s * 0.30), int(s * 0.86), int(s * 0.70), int(s * 0.86)], fill=fg_hex, width=w)
        else:
            draw.arc([int(s * 0.16), int(s * 0.14), int(s * 0.84), int(s * 0.84)], start=180, end=0, fill=fg_hex, width=w)
            draw.rounded_rectangle([int(s * 0.14), int(s * 0.48), int(s * 0.32), int(s * 0.84)], radius=10, fill=fg_hex)
            draw.rounded_rectangle([int(s * 0.68), int(s * 0.48), int(s * 0.86), int(s * 0.84)], radius=10, fill=fg_hex)

        img = img.resize((size, size), RESAMPLE_LANCZOS)
        photo = ImageTk.PhotoImage(img)
        self.vector_cache[key] = photo
        return photo

    def draw_gear_icon(self, bg_hex, fg_hex, size=28):
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), bg_hex)
        draw = ImageDraw.Draw(img)
        cx, cy = s / 2, s / 2
        w = max(6, int(s * 0.08))

        for i in range(8):
            angle = i * (math.pi / 4)
            x1 = cx + math.cos(angle) * (s * 0.24)
            y1 = cy + math.sin(angle) * (s * 0.24)
            x2 = cx + math.cos(angle) * (s * 0.40)
            y2 = cy + math.sin(angle) * (s * 0.40)
            draw.line([(x1, y1), (x2, y2)], fill=fg_hex, width=int(s * 0.12))

        draw.ellipse([int(s * 0.22), int(s * 0.22), int(s * 0.78), int(s * 0.78)], fill=bg_hex, outline=fg_hex, width=w)
        draw.ellipse([int(s * 0.36), int(s * 0.36), int(s * 0.64), int(s * 0.64)], fill=bg_hex, outline=fg_hex, width=max(4, w // 2))
        img = img.resize((size, size), RESAMPLE_LANCZOS)
        return ImageTk.PhotoImage(img)

    def draw_system_audio_icon(self, bg_hex, size=36):
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), bg_hex)
        draw = ImageDraw.Draw(img)
        w = max(6, int(s * 0.06))
        draw.rounded_rectangle([int(s * 0.10), int(s * 0.16), int(s * 0.90), int(s * 0.70)], radius=16, fill="#252836", outline="#38BDF8", width=w)
        draw.line([int(s * 0.50), int(s * 0.70), int(s * 0.50), int(s * 0.86)], fill="#9499B3", width=w)
        draw.line([int(s * 0.30), int(s * 0.86), int(s * 0.70), int(s * 0.86)], fill="#9499B3", width=w)
        draw.polygon([
            (int(s * 0.36), int(s * 0.38)), (int(s * 0.44), int(s * 0.38)),
            (int(s * 0.54), int(s * 0.28)), (int(s * 0.54), int(s * 0.58)),
            (int(s * 0.44), int(s * 0.48)), (int(s * 0.36), int(s * 0.48))
        ], fill="#FFFFFF")
        draw.arc([int(s * 0.50), int(s * 0.32), int(s * 0.68), int(s * 0.54)], start=-55, end=55, fill="#38BDF8", width=w)
        img = img.resize((size, size), RESAMPLE_LANCZOS)
        return ImageTk.PhotoImage(img)

    def draw_vector_speaker(self, bg_hex, fg_hex, muted=False, size=36):
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), bg_hex)
        draw = ImageDraw.Draw(img)
        w = max(6, int(s * 0.065))

        p1 = (int(s * 0.14), int(s * 0.36))
        p2 = (int(s * 0.28), int(s * 0.36))
        p3 = (int(s * 0.48), int(s * 0.18))
        p4 = (int(s * 0.48), int(s * 0.82))
        p5 = (int(s * 0.28), int(s * 0.64))
        p6 = (int(s * 0.14), int(s * 0.64))
        draw.polygon([p1, p2, p3, p4, p5, p6], outline=fg_hex, fill=None, width=w)

        if muted:
            x1, y1 = int(s * 0.60), int(s * 0.35)
            x2, y2 = int(s * 0.86), int(s * 0.65)
            draw.line([(x1, y1), (x2, y2)], fill=fg_hex, width=w)
            draw.line([(x1, y2), (x2, y1)], fill=fg_hex, width=w)
        else:
            draw.arc([int(s * 0.45), int(s * 0.30), int(s * 0.70), int(s * 0.70)], start=-55, end=55, fill=fg_hex, width=w)
            draw.arc([int(s * 0.52), int(s * 0.18), int(s * 0.88), int(s * 0.82)], start=-55, end=55, fill=fg_hex, width=w)

        img = img.resize((size, size), RESAMPLE_LANCZOS)
        return ImageTk.PhotoImage(img)

    def extract_high_res_icon(self, exe_path, icon_index=0, bg_hex="#181920", size=36):
        cache_key = f"{exe_path}:{icon_index}:{bg_hex}:{size}"
        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]

        if not exe_path or not os.path.exists(exe_path):
            self.icon_cache[cache_key] = self.default_app_icon
            return self.default_app_icon

        lower_path = exe_path.lower()

        if "whatsapp" in lower_path:
            uwp_ic = self.extract_uwp_manifest_icon(exe_path, bg_hex, size)
            photo = uwp_ic if uwp_ic else self.whatsapp_icon
            self.icon_cache[cache_key] = photo
            return photo

        if "windowsapps" in lower_path:
            uwp_ic = self.extract_uwp_manifest_icon(exe_path, bg_hex, size)
            if uwp_ic:
                self.icon_cache[cache_key] = uwp_ic
                return uwp_ic

        screen_dc = 0
        hdc = None
        hdc_mem = None
        hbmp = None
        hicon_val = 0

        try:
            extract_size = 128
            hicon = c_void_p()
            icon_id = ctypes.c_uint()
            res = user32.PrivateExtractIconsW(
                c_wchar_p(exe_path), icon_index, extract_size, extract_size,
                byref(hicon), byref(icon_id), 1, 0
            )

            if res == 0 or not hicon.value:
                large, small = win32gui.ExtractIconEx(exe_path, icon_index)
                if not large and not small:
                    self.icon_cache[cache_key] = self.default_app_icon
                    return self.default_app_icon
                hicon_val = large[0] if large else small[0]
                for h in (large[1:] if large else []) + (small if large else small[1:]):
                    win32gui.DestroyIcon(h)
                extract_size = 32
            else:
                hicon_val = hicon.value

            screen_dc = win32gui.GetDC(0)
            hdc = win32ui.CreateDCFromHandle(screen_dc)
            hbmp = win32ui.CreateBitmap()
            hbmp.CreateCompatibleBitmap(hdc, extract_size, extract_size)
            hdc_mem = hdc.CreateCompatibleDC()
            hdc_mem.SelectObject(hbmp)

            h_clean = bg_hex.lstrip("#")
            r_c, g_c, b_c = int(h_clean[0:2], 16), int(h_clean[2:4], 16), int(h_clean[4:6], 16)
            hdc_mem.FillSolidRect((0, 0, extract_size, extract_size), win32api.RGB(r_c, g_c, b_c))

            win32gui.DrawIconEx(
                hdc_mem.GetHandleOutput(), 0, 0, hicon_val,
                extract_size, extract_size, 0, None, win32con.DI_NORMAL
            )

            bmpstr = hbmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (extract_size, extract_size), bmpstr, "raw", "BGRX", 0, 1)
            img = img.resize((size, size), RESAMPLE_LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.icon_cache[cache_key] = photo
            return photo
        except Exception:
            logging.exception("Icon extraction failed for %s", exe_path)
            self.icon_cache[cache_key] = self.default_app_icon
            return self.default_app_icon
        finally:
            if hicon_val:
                try:
                    win32gui.DestroyIcon(hicon_val)
                except Exception:
                    pass
            if hdc_mem:
                try:
                    hdc_mem.DeleteDC()
                except Exception:
                    pass
            if hdc:
                try:
                    hdc.DeleteDC()
                except Exception:
                    pass
            if hbmp:
                try:
                    win32gui.DeleteObject(hbmp.GetHandle())
                except Exception:
                    pass
            if screen_dc:
                try:
                    win32gui.ReleaseDC(0, screen_dc)
                except Exception:
                    pass

    # -----------------------------------------------------------------
    # Cached Audio Engine & USB DAC Protection
    # -----------------------------------------------------------------
    def _clear_master_vol_cache(self):
        self._master_vol = None

    def get_master_volume_interface(self):
        if self._master_vol is not None:
            return self._master_vol
        try:
            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):
                self._master_vol = speakers.EndpointVolume
            else:
                dev = getattr(speakers, "_dev", speakers)
                iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self._master_vol = comtypes.cast(iface, POINTER(IAudioEndpointVolume))
            return self._master_vol
        except Exception:
            logging.exception("Failed to acquire Master Volume COM interface")
            self._master_vol = None
            return None

    def wake_up_current_endpoint(self):
        try:
            m_vol = self.get_master_volume_interface()
            if m_vol and not m_vol.GetMute():
                cur_lvl = m_vol.GetMasterVolumeLevelScalar()
                m_vol.SetMute(0, None)
                m_vol.SetMasterVolumeLevelScalar(cur_lvl, None)
        except Exception:
            logging.exception("Failed to wake endpoint")

    def fetch_devices(self):
        self.devices = []
        try:
            speakers = AudioUtilities.GetSpeakers()
            self.current_device_id = getattr(speakers, "id", None) or (speakers.GetId() if hasattr(speakers, "GetId") else None)
        except Exception:
            self.current_device_id = None

        try:
            for dev in AudioUtilities.GetAllDevices():
                state_val = getattr(dev.state, "value", dev.state)
                if state_val == 1 and dev.id.startswith("{0.0.0."):
                    self.devices.append((dev.id, dev.FriendlyName))
        except Exception:
            logging.exception("Failed to enumerate audio devices")

    def get_current_device_name(self):
        for d_id, name in self.devices:
            if d_id == self.current_device_id:
                return name
        return "Default Audio Output"

    def set_default_device(self, device_id, show_osd=False):
        try:
            policy_config = CoCreateInstance(
                CLSID_PolicyConfigClient,
                interface=IPolicyConfig,
                clsctx=CLSCTX_ALL
            )
            for role in (0, 1, 2):
                policy_config.SetDefaultEndpoint(device_id, role)

            self._master_vol = None
            self.fetch_devices()
            self.current_device_id = device_id
            self.wake_up_current_endpoint()
            self.root.after(300, self._clear_master_vol_cache)

            if self.is_visible:
                self.build_flyout_ui()
                self.position_flyout()
            if show_osd:
                self.show_top_left_osd()
        except Exception:
            logging.exception("Failed to switch default audio device")

    def cycle_audio_device(self, show_osd=True):
        self.fetch_devices()
        if len(self.devices) < 2:
            return
        excluded = set(self.config.get("excluded_devices", []))
        active_ids = [d[0] for d in self.devices if d[0] not in excluded]
        if len(active_ids) < 2:
            active_ids = [d[0] for d in self.devices]

        next_idx = (active_ids.index(self.current_device_id) + 1) % len(active_ids) if self.current_device_id in active_ids else 0
        self.set_default_device(active_ids[next_idx], show_osd=show_osd)

    # -----------------------------------------------------------------
    # Top-Left OSD Notification Banner
    # -----------------------------------------------------------------
    def show_top_left_osd(self, title="Playback Device", subtitle=None):
        if self.osd_win is not None:
            try:
                self.osd_win.destroy()
            except Exception:
                pass

        dev_name = subtitle if subtitle else self.get_current_device_name()
        icon_type = self.get_device_icon_type(self.current_device_id, dev_name)
        osd_icon = self.get_device_vector_icon(icon_type, THEME["osd_bg"], "#FFFFFF", size=int(32 * self.dpi_scale))

        self.osd_win = tk.Toplevel(self.root)
        self.osd_win.overrideredirect(True)
        self.osd_win.attributes("-topmost", True)
        self.osd_win.attributes("-alpha", 0.94)
        self.osd_win.configure(bg=THEME["border"])
        self.apply_win11_rounded_corners(self.osd_win)
        self.apply_noactivate_style(self.osd_win)

        inner = tk.Frame(self.osd_win, bg=THEME["osd_bg"], padx=16, pady=12)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        icon_lbl = tk.Label(inner, image=osd_icon, bg=THEME["osd_bg"])
        icon_lbl.image = osd_icon
        icon_lbl.pack(side="left", padx=(0, 14))

        txt_frame = tk.Frame(inner, bg=THEME["osd_bg"])
        txt_frame.pack(side="left", fill="both", expand=True)

        title_col = THEME["mute"] if "Conflict" in title else THEME["text_dim"]
        tk.Label(
            txt_frame, text=title, bg=THEME["osd_bg"],
            fg=title_col, font=("Segoe UI", 9, "bold"), anchor="w"
        ).pack(fill="x")

        tk.Label(
            txt_frame, text=dev_name, bg=THEME["osd_bg"],
            fg=THEME["text_main"], font=("Segoe UI", 11), anchor="w"
        ).pack(fill="x", pady=(2, 0))

        self.osd_win.update_idletasks()
        w = max(int(270 * self.dpi_scale), self.osd_win.winfo_reqwidth())
        h = self.osd_win.winfo_reqheight()

        work_left, work_top, _, _ = self.get_active_monitor_work_area()
        self.osd_win.geometry(f"{w}x{h}+{work_left + 20}+{work_top + 20}")

        if self.osd_after_id:
            try:
                self.root.after_cancel(self.osd_after_id)
            except Exception:
                pass
        self.osd_after_id = self.root.after(1800, self._destroy_osd)

    def _destroy_osd(self):
        if self.osd_win is not None:
            try:
                self.osd_win.destroy()
            except Exception:
                pass
            self.osd_win = None

    # -----------------------------------------------------------------
    # Custom Dark Right-Click Tray Menu & About Dialog
    # -----------------------------------------------------------------
    def close_tray_menu(self):
        if self.tray_menu_win is not None:
            try:
                self.tray_menu_win.destroy()
            except Exception:
                pass
            self.tray_menu_win = None

    def show_custom_tray_menu(self):
        self.close_tray_menu()
        self.fetch_devices()
        cur_dev = self.get_current_device_name()

        cx, cy = win32api.GetCursorPos()

        menu_win = tk.Toplevel(self.root)
        self.tray_menu_win = menu_win
        menu_win.overrideredirect(True)
        menu_win.attributes("-topmost", True)
        menu_win.attributes("-alpha", 0.96)
        menu_win.configure(bg=THEME["border"])
        self.apply_win11_rounded_corners(menu_win)

        container = tk.Frame(menu_win, bg=THEME["menu_bg"], padx=8, pady=8)
        container.pack(fill="both", expand=True, padx=1, pady=1)

        # 1. Brand Header inside Right-Click Menu
        brand_hdr = tk.Frame(container, bg="#222534", padx=10, pady=8)
        brand_hdr.pack(fill="x", pady=(0, 6))

        logo_lbl = tk.Label(brand_hdr, image=self.logo_menu_tk, bg="#222534")
        logo_lbl.pack(side="left", padx=(0, 8))

        title_box = tk.Frame(brand_hdr, bg="#222534")
        title_box.pack(side="left", fill="x", expand=True)

        top_line = tk.Frame(title_box, bg="#222534")
        top_line.pack(fill="x")
        tk.Label(top_line, text="AudioHub", bg="#222534", fg="#FFFFFF", font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(top_line, text=" BETA ", bg=THEME["badge_bg"], fg=THEME["accent_bright"], font=("Segoe UI", 7, "bold"), padx=4, pady=1).pack(side="right")

        tk.Label(title_box, text=f"🎧 {cur_dev[:26]}", bg="#222534", fg=THEME["text_dim"], font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(2, 0))

        # Helper to build modern interactive menu rows
        def add_menu_item(icon_char, label_text, badge_text, callback, danger=False):
            row = tk.Frame(container, bg=THEME["menu_bg"], padx=10, pady=7, cursor="hand2")
            row.pack(fill="x", pady=1)

            fg_norm = THEME["mute"] if danger else THEME["text_main"]
            ic_lbl = tk.Label(row, text=icon_char, bg=THEME["menu_bg"], fg=THEME["accent_bright"] if not danger else THEME["mute"], font=("Segoe UI", 10, "bold"), width=2, anchor="w", cursor="hand2")
            ic_lbl.pack(side="left", padx=(0, 6))

            txt_lbl = tk.Label(row, text=label_text, bg=THEME["menu_bg"], fg=fg_norm, font=("Segoe UI", 9, "bold" if icon_char == "🎚" else "normal"), anchor="w", cursor="hand2")
            txt_lbl.pack(side="left", fill="x", expand=True)

            bdg_lbl = None
            if badge_text:
                bdg_lbl = tk.Label(row, text=badge_text.upper(), bg="#262936", fg=THEME["text_dim"], font=("Segoe UI", 8, "bold"), padx=6, pady=1, cursor="hand2")
                bdg_lbl.pack(side="right", padx=(8, 0))

            widgets = [row, ic_lbl, txt_lbl] + ([bdg_lbl] if bdg_lbl else [])

            def on_enter(e):
                hov_bg = "#3A1D24" if danger else THEME["menu_hover"]
                for w in widgets:
                    if w is not bdg_lbl:
                        w.config(bg=hov_bg)

            def on_leave(e):
                for w in widgets:
                    if w is not bdg_lbl:
                        w.config(bg=THEME["menu_bg"])

            def on_click(e):
                self.close_tray_menu()
                callback()

            for w in widgets:
                w.bind("<Enter>", on_enter)
                w.bind("<Leave>", on_leave)
                w.bind("<Button-1>", on_click)

        hk_mix = self.config.get("hotkey_mixer", "alt+q")
        hk_sw = self.config.get("hotkey_switch", "alt+w")

        add_menu_item("🎚", "Open Volume Mixer", hk_mix, lambda: self.show_flyout())
        add_menu_item("⇄", "Switch Audio Device", hk_sw, lambda: self.cycle_audio_device(show_osd=True))
        add_menu_item("⚙", "Settings & Shortcuts", "", lambda: self.show_flyout(expand_settings=True))
        add_menu_item("ⓘ", f"About AudioHub ({APP_VERSION})", "", lambda: self.show_about_dialog())

        tk.Frame(container, bg=THEME["border"], height=1).pack(fill="x", pady=5)
        add_menu_item("✕", "Exit AudioHub", "", lambda: self.quit_app(), danger=True)

        menu_win.update_idletasks()
        mw = max(int(265 * self.dpi_scale), menu_win.winfo_reqwidth())
        mh = menu_win.winfo_reqheight()

        work_left, work_top, work_right, work_bottom = self.get_active_monitor_work_area()
        pos_x = min(max(work_left + 8, cx - mw + 20), work_right - mw - 8)
        pos_y = min(max(work_top + 8, cy - mh - 8), work_bottom - mh - 8)
        menu_win.geometry(f"{mw}x{mh}+{pos_x}+{pos_y}")

        menu_win.lift()
        menu_win.focus_force()
        menu_win.bind("<FocusOut>", lambda e: self.root.after(80, self.close_tray_menu))
        menu_win.bind("<Escape>", lambda e: self.close_tray_menu())

    def show_about_dialog(self):
        self.close_tray_menu()
        if self.about_win is not None and self.about_win.winfo_exists():
            self.about_win.lift()
            self.about_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.about_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.97)
        win.configure(bg=THEME["border"])
        self.apply_win11_rounded_corners(win)

        box = tk.Frame(win, bg=THEME["body_bg"], padx=24, pady=20)
        box.pack(fill="both", expand=True, padx=1, pady=1)

        logo_lbl = tk.Label(box, image=self.logo_large_tk, bg=THEME["body_bg"])
        logo_lbl.pack(pady=(0, 10))

        tk.Label(box, text="AudioHub", bg=THEME["body_bg"], fg="#FFFFFF", font=("Segoe UI", 15, "bold")).pack()

        badge_row = tk.Frame(box, bg=THEME["body_bg"])
        badge_row.pack(pady=(4, 10))
        tk.Label(badge_row, text=f"  {APP_VERSION}  ", bg=THEME["badge_bg"], fg=THEME["accent_bright"], font=("Segoe UI", 9, "bold"), padx=8, pady=2).pack()

        desc = (
            "All-in-One Windows Per-App Volume Mixer\n"
            "& Instant Playback Device Switcher.\n\n"
            "• Core Audio & IPolicyConfig COM Engine\n"
            "• Zero-CPU Native Win32 Global Hotkeys\n"
            "• Open-Source Community Edition"
        )
        tk.Label(box, text=desc, bg=THEME["body_bg"], fg=THEME["text_dim"], font=("Segoe UI", 9), justify="center").pack(pady=(0, 14))

        close_btn = tk.Label(
            box, text="Close", bg=THEME["dropdown_active"], fg="#FFFFFF",
            font=("Segoe UI", 9, "bold"), padx=24, pady=6, cursor="hand2"
        )
        close_btn.pack()
        close_btn.bind("<Button-1>", lambda e: win.destroy())
        close_btn.bind("<Enter>", lambda e: close_btn.config(bg=THEME["accent_bright"], fg="#11111B"))
        close_btn.bind("<Leave>", lambda e: close_btn.config(bg=THEME["dropdown_active"], fg="#FFFFFF"))

        win.update_idletasks()
        w = max(int(320 * self.dpi_scale), win.winfo_reqwidth())
        h = win.winfo_reqheight()
        work_left, work_top, work_right, work_bottom = self.get_active_monitor_work_area()
        cx = work_left + (work_right - work_left - w) // 2
        cy = work_top + (work_bottom - work_top - h) // 2
        win.geometry(f"{w}x{h}+{cx}+{cy}")
        win.lift()
        win.focus_force()
        win.bind("<Escape>", lambda e: win.destroy())

    # -----------------------------------------------------------------
    # Flyout UI Construction
    # -----------------------------------------------------------------
    def _on_body_mousewheel(self, event):
        if self.body_canvas and self.apps_frame:
            if self.apps_frame.winfo_reqheight() > self.body_canvas.winfo_height():
                step = -1 if event.delta > 0 else 1
                self.body_canvas.yview_scroll(step, "units")

    def _bind_scroll_recursive(self, widget):
        if not isinstance(widget, ModernSlider):
            widget.bind("<MouseWheel>", self._on_body_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_scroll_recursive(child)

    def build_flyout_ui(self):
        self.master_sync_ref = None
        self.app_sync_refs = []

        for w in self.main_frame.winfo_children():
            w.destroy()

        self.fetch_devices()
        self._last_topology_sig = self._compute_audio_topology_signature()
        current_name = self.get_current_device_name()
        current_icon_type = self.get_device_icon_type(self.current_device_id, current_name)

        pad_x = int(18 * self.dpi_scale)
        pad_y = int(14 * self.dpi_scale)

        # 1. Header Container
        self.header_frame = tk.Frame(self.main_frame, bg=THEME["header_bg"], padx=pad_x, pady=pad_y)
        self.header_frame.pack(fill="x")

        dev_bar = tk.Frame(self.header_frame, bg=THEME["dev_btn_bg"], padx=10, pady=7)
        dev_bar.pack(fill="x", pady=(0, 12))

        dev_vec_img = self.get_device_vector_icon(current_icon_type, THEME["dev_btn_bg"], "#FFFFFF", size=int(26 * self.dpi_scale))
        dev_icon_lbl = tk.Label(dev_bar, image=dev_vec_img, bg=THEME["dev_btn_bg"], cursor="hand2")
        dev_icon_lbl.pack(side="left", padx=(2, 8))
        dev_icon_lbl.bind("<Button-1>", lambda e: self.cycle_device_icon_type(self.current_device_id, current_name))

        dev_lbl = tk.Label(
            dev_bar, text=current_name[:30], bg=THEME["dev_btn_bg"], fg=THEME["text_main"],
            font=("Segoe UI", 11, "bold"), anchor="w", cursor="hand2"
        )
        dev_lbl.pack(side="left", fill="x", expand=True)

        gear_bg = THEME["dropdown_active"] if self.settings_expanded else THEME["dev_btn_bg"]
        gear_img = self.gear_icon_active if self.settings_expanded else self.gear_icon_normal
        cfg_btn = tk.Label(dev_bar, image=gear_img, bg=gear_bg, cursor="hand2", padx=4, pady=2)
        cfg_btn.pack(side="right", padx=(4, 0))
        cfg_btn.bind("<Button-1>", lambda e: self.toggle_inline_settings())
        cfg_btn.bind("<Enter>", lambda e: cfg_btn.config(image=self.gear_icon_hover, bg=THEME["dev_btn_hover"]) if not self.settings_expanded else None)
        cfg_btn.bind("<Leave>", lambda e: cfg_btn.config(image=self.gear_icon_normal, bg=THEME["dev_btn_bg"]) if not self.settings_expanded else None)

        quick_switch_btn = tk.Label(
            dev_bar, text="⇄ Switch", bg=THEME["dropdown_active"], fg=THEME["text_main"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=3, cursor="hand2"
        )
        quick_switch_btn.pack(side="right", padx=(6, 4))
        quick_switch_btn.bind("<Button-1>", lambda e: self.cycle_audio_device(show_osd=False))
        quick_switch_btn.bind("<Enter>", lambda e: quick_switch_btn.config(bg=THEME["accent_bright"], fg="#11111B"))
        quick_switch_btn.bind("<Leave>", lambda e: quick_switch_btn.config(bg=THEME["dropdown_active"], fg=THEME["text_main"]))

        arrow_txt = "▴" if self.device_list_expanded else "▾"
        chevron = tk.Label(
            dev_bar, text=arrow_txt, bg=THEME["dev_btn_bg"], fg=THEME["text_main"],
            font=("Segoe UI", 12, "bold"), cursor="hand2", padx=6
        )
        chevron.pack(side="right")

        for widget in (dev_lbl, chevron):
            widget.bind("<Button-1>", lambda e: self.toggle_device_dropdown())

        # Expanded Device Switcher Cards
        if self.device_list_expanded and len(self.devices) > 0:
            drop_frame = tk.Frame(self.header_frame, bg=THEME["dropdown_bg"], padx=8, pady=8)
            drop_frame.pack(fill="x", pady=(0, 12))

            tk.Label(
                drop_frame, text="SELECT PLAYBACK DEVICE", bg=THEME["dropdown_bg"],
                fg=THEME["text_dim"], font=("Segoe UI", 8, "bold"), anchor="w", padx=4, pady=2
            ).pack(fill="x")

            for d_id, d_name in self.devices:
                is_curr = (d_id == self.current_device_id)
                card_bg = THEME["dropdown_active"] if is_curr else THEME["dropdown_bg"]
                d_type = self.get_device_icon_type(d_id, d_name)
                d_img = self.get_device_vector_icon(d_type, card_bg, THEME["text_main"] if is_curr else THEME["text_dim"], size=int(22 * self.dpi_scale))

                card = tk.Frame(drop_frame, bg=card_bg, padx=8, pady=6, cursor="hand2")
                card.pack(fill="x", pady=2)

                ic = tk.Label(card, image=d_img, bg=card_bg, cursor="hand2")
                ic.pack(side="left", padx=(0, 8))

                lbl = tk.Label(
                    card, text=d_name[:38], bg=card_bg,
                    fg=THEME["text_main"] if is_curr else THEME["text_dim"],
                    font=("Segoe UI", 10, "bold" if is_curr else "normal"),
                    anchor="w", cursor="hand2"
                )
                lbl.pack(side="left", fill="x", expand=True)

                for w_item in (card, ic, lbl):
                    w_item.bind("<Button-1>", lambda e, target=d_id: self.select_device_from_list(target))

        # Inline Expandable Settings Panel
        if self.settings_expanded:
            self.build_inline_settings_panel(self.header_frame)

        # Master Volume Row inside Header
        master_vol = self.get_master_volume_interface()
        if master_vol is not None:
            try:
                m_val = int(round(master_vol.GetMasterVolumeLevelScalar() * 100))
                m_muted = bool(master_vol.GetMute())
                master_state = {"last_nonzero": m_val if m_val > 0 else 50}

                m_row = tk.Frame(self.header_frame, bg=THEME["header_bg"])
                m_row.pack(fill="x", pady=(2, 2))

                m_icon = tk.Label(m_row, image=self.speaker_header_icon, bg=THEME["header_bg"], cursor="hand2")
                m_icon.pack(side="left", padx=(0, 14))

                m_status = tk.Label(m_row, bg=THEME["header_bg"], width=4, anchor="e", cursor="hand2")
                m_status.pack(side="right")

                m_slider = ModernSlider(
                    m_row, self, value=m_val, muted=(m_muted or m_val == 0), bg_color=THEME["header_bg"],
                    accent_color=THEME["accent"], width=int(320 * self.dpi_scale), height=self.slider_height
                )
                m_slider.pack(side="left", fill="x", expand=True)

                def update_master_ui(muted_state, current_v):
                    if current_v > 0:
                        master_state["last_nonzero"] = current_v
                    is_silent = muted_state or (current_v == 0)
                    if is_silent:
                        m_status.config(image=self.header_red_mute_small, text="", width=self.mute_icon_size + 4)
                    else:
                        m_status.config(image="", text=str(current_v), fg=THEME["text_main"], font=("Segoe UI", 14), width=4)
                    m_slider.set_state(current_v, is_silent)

                update_master_ui(m_muted, m_val)

                def on_master_slide(v):
                    try:
                        mv = self.get_master_volume_interface()
                        if mv:
                            mv.SetMasterVolumeLevelScalar(v / 100.0, None)
                            if v == 0:
                                mv.SetMute(1, None)
                            elif mv.GetMute():
                                mv.SetMute(0, None)
                    except Exception:
                        logging.exception("Failed to set master volume")
                        self._master_vol = None
                    update_master_ui(v == 0, v)

                def on_master_mute(e):
                    try:
                        mv = self.get_master_volume_interface()
                        if mv:
                            cur_v = int(round(mv.GetMasterVolumeLevelScalar() * 100))
                            cur_m = bool(mv.GetMute())
                            if cur_m or cur_v == 0:
                                restore_v = master_state["last_nonzero"] if cur_v == 0 else cur_v
                                mv.SetMasterVolumeLevelScalar(restore_v / 100.0, None)
                                mv.SetMute(0, None)
                                update_master_ui(False, restore_v)
                            else:
                                mv.SetMute(1, None)
                                update_master_ui(True, cur_v)
                    except Exception:
                        logging.exception("Failed to toggle master mute")
                        self._master_vol = None

                m_slider.command = on_master_slide
                m_icon.bind("<Button-1>", on_master_mute)
                m_status.bind("<Button-1>", on_master_mute)

                self.master_sync_ref = {"update_ui": update_master_ui}
            except Exception:
                logging.exception("Failed to build master volume row")

        # 2. Scrollable App Sessions Container
        self.body_canvas = tk.Canvas(self.main_frame, bg=THEME["body_bg"], highlightthickness=0, bd=0)
        self.body_canvas.pack(fill="both", expand=True)

        self.apps_frame = tk.Frame(self.body_canvas, bg=THEME["body_bg"], padx=pad_x, pady=int(12 * self.dpi_scale))
        self._canvas_win_id = self.body_canvas.create_window((0, 0), window=self.apps_frame, anchor="nw", width=self.flyout_width - 2)

        self.apps_frame.bind("<Configure>", lambda e: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")))

        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            logging.exception("GetAllSessions failed")
            sessions = []

        grouped = {}
        system_sessions = []

        HOST_PROCS = {"msedgewebview2.exe", "runtimebroker.exe", "applicationframehost.exe"}
        STOP_PROCS = {"svchost.exe", "explorer.exe", "services.exe", "wininit.exe"}

        for s in sessions:
            try:
                proc = s.Process
                pid = getattr(s, "ProcessId", None)

                if proc is not None:
                    cur = proc
                    for _ in range(6):
                        try:
                            c_name = cur.name().lower()
                        except Exception:
                            break
                        if c_name not in HOST_PROCS:
                            break
                        try:
                            parent = cur.parent()
                            if not parent or parent.name().lower() in STOP_PROCS:
                                break
                            cur = parent
                        except Exception:
                            break

                    try:
                        proc_name = cur.name().lower()
                        exe_path = cur.exe()
                    except Exception:
                        proc_name = "app"
                        exe_path = None

                    if "steam" in proc_name:
                        key = "steam_group"
                    elif "whatsapp" in proc_name or (exe_path and "whatsapp" in exe_path.lower()):
                        key = "whatsapp_group"
                    else:
                        key = exe_path.lower() if exe_path else proc_name

                    if key not in grouped:
                        grouped[key] = {"exe": exe_path, "sessions": []}
                    elif grouped[key]["exe"] is None and exe_path:
                        grouped[key]["exe"] = exe_path
                    grouped[key]["sessions"].append(s)
                elif pid == 0:
                    system_sessions.append(s)
            except Exception:
                logging.exception("Error grouping audio session")

        if system_sessions and self.config.get("show_system_sounds", True):
            try:
                self.create_app_row(self.apps_frame, None, system_sessions, custom_icon=self.system_sounds_icon)
            except Exception:
                logging.exception("Failed to create System Sounds row")

        for key, info in grouped.items():
            try:
                custom_ic = self.whatsapp_icon if key == "whatsapp_group" else None
                self.create_app_row(self.apps_frame, info["exe"], info["sessions"], custom_icon=custom_ic)
            except Exception:
                logging.exception("Failed to create app row for %s", key)

        self._bind_scroll_recursive(self.apps_frame)

    def build_inline_settings_panel(self, parent_frame):
        panel = tk.Frame(parent_frame, bg=THEME["settings_bg"], padx=12, pady=10)
        panel.pack(fill="x", pady=(0, 12))

        tk.Label(
            panel, text="KEYBOARD SHORTCUTS (SINGLE KEY OR MODIFIER + KEY)",
            bg=THEME["settings_bg"], fg=THEME["text_dim"], font=("Segoe UI", 8, "bold"), anchor="w"
        ).pack(fill="x", pady=(0, 6))

        hk_row1 = tk.Frame(panel, bg=THEME["settings_bg"])
        hk_row1.pack(fill="x", pady=3)
        tk.Label(hk_row1, text="Open Mixer Flyout:", bg=THEME["settings_bg"], fg=THEME["text_main"], font=("Segoe UI", 10)).pack(side="left")

        hk1_val = self.config.get("hotkey_mixer", "alt+q").upper()
        btn_hk1 = tk.Label(
            hk_row1, text=f"  {hk1_val}  ", bg=THEME["card_bg"], fg=THEME["accent_bright"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=4, cursor="hand2"
        )
        btn_hk1.pack(side="right")
        btn_hk1.bind("<Button-1>", lambda e: self.start_recording_hotkey("hotkey_mixer", btn_hk1))

        hk_row2 = tk.Frame(panel, bg=THEME["settings_bg"])
        hk_row2.pack(fill="x", pady=3)
        tk.Label(hk_row2, text="Switch Audio Device:", bg=THEME["settings_bg"], fg=THEME["text_main"], font=("Segoe UI", 10)).pack(side="left")

        hk2_val = self.config.get("hotkey_switch", "alt+w").upper()
        btn_hk2 = tk.Label(
            hk_row2, text=f"  {hk2_val}  ", bg=THEME["card_bg"], fg=THEME["accent_bright"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=4, cursor="hand2"
        )
        btn_hk2.pack(side="right")
        btn_hk2.bind("<Button-1>", lambda e: self.start_recording_hotkey("hotkey_switch", btn_hk2))

        sys_row = tk.Frame(panel, bg=THEME["settings_bg"])
        sys_row.pack(fill="x", pady=(6, 2))
        tk.Label(sys_row, text="Show System Sounds Row:", bg=THEME["settings_bg"], fg=THEME["text_main"], font=("Segoe UI", 10)).pack(side="left")
        show_sys = bool(self.config.get("show_system_sounds", True))
        btn_sys = tk.Label(
            sys_row, text="  ON  " if show_sys else "  OFF  ",
            bg=THEME["card_bg"], fg="#A6E3A1" if show_sys else THEME["text_dim"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=3, cursor="hand2"
        )
        btn_sys.pack(side="right")
        btn_sys.bind("<Button-1>", lambda e: self.toggle_show_system_sounds())

        tk.Frame(panel, bg=THEME["border"], height=1).pack(fill="x", pady=8)

        tk.Label(
            panel, text="DEVICES (CHANGE ICON OR INCLUDE IN QUICK SWITCH)",
            bg=THEME["settings_bg"], fg=THEME["text_dim"], font=("Segoe UI", 8, "bold"), anchor="w"
        ).pack(fill="x", pady=(0, 6))

        excluded_list = self.config.get("excluded_devices", [])
        for d_id, d_name in self.devices:
            d_row = tk.Frame(panel, bg=THEME["settings_bg"])
            d_row.pack(fill="x", pady=3)

            is_included = d_id not in excluded_list
            chk_lbl = tk.Label(
                d_row, text="☑" if is_included else "☐",
                bg=THEME["settings_bg"], fg="#A6E3A1" if is_included else THEME["text_dim"],
                font=("Segoe UI", 11), cursor="hand2"
            )
            chk_lbl.pack(side="left", padx=(0, 6))
            chk_lbl.bind("<Button-1>", lambda e, tid=d_id: self.toggle_device_exclusion(tid))

            tk.Label(
                d_row, text=d_name[:25], bg=THEME["settings_bg"],
                fg=THEME["text_main"] if is_included else THEME["text_dim"], font=("Segoe UI", 9), anchor="w"
            ).pack(side="left", fill="x", expand=True)

            cur_type = self.get_device_icon_type(d_id, d_name)
            btn_frame = tk.Frame(d_row, bg=THEME["card_bg"], padx=8, pady=3, cursor="hand2")
            btn_frame.pack(side="right")

            ic_img = self.get_device_vector_icon(cur_type, THEME["card_bg"], THEME["accent_bright"], size=int(18 * self.dpi_scale))
            ic_lbl = tk.Label(btn_frame, image=ic_img, bg=THEME["card_bg"], cursor="hand2")
            ic_lbl.pack(side="left", padx=(0, 6))

            txt_lbl = tk.Label(
                btn_frame, text=cur_type.capitalize(), bg=THEME["card_bg"],
                fg=THEME["text_main"], font=("Segoe UI", 9, "bold"), cursor="hand2"
            )
            txt_lbl.pack(side="left")

            for w_el in (btn_frame, ic_lbl, txt_lbl):
                w_el.bind("<Button-1>", lambda e, target_id=d_id, target_name=d_name: self.cycle_device_icon_type(target_id, target_name))

        # Inline About Footer inside Settings
        tk.Frame(panel, bg=THEME["border"], height=1).pack(fill="x", pady=(8, 6))
        abt_row = tk.Frame(panel, bg=THEME["settings_bg"])
        abt_row.pack(fill="x")

        tk.Label(
            abt_row, text=f"AudioHub {APP_VERSION}",
            bg=THEME["settings_bg"], fg=THEME["text_dim"], font=("Segoe UI", 8, "bold")
        ).pack(side="left")

        abt_btn = tk.Label(
            abt_row, text="ⓘ About", bg=THEME["card_bg"], fg=THEME["accent_bright"],
            font=("Segoe UI", 8, "bold"), padx=8, pady=2, cursor="hand2"
        )
        abt_btn.pack(side="right")
        abt_btn.bind("<Button-1>", lambda e: self.show_about_dialog())

    def create_app_row(self, parent, exe_path, app_sessions, custom_icon=None):
        first_ctl = app_sessions[0]._ctl.QueryInterface(ISimpleAudioVolume)
        val = int(round(first_ctl.GetMasterVolume() * 100))
        is_muted = bool(first_ctl.GetMute())
        row_state = {"last_nonzero": val if val > 0 else 50}

        row = tk.Frame(parent, bg=THEME["body_bg"], pady=int(8 * self.dpi_scale))
        row.pack(fill="x")

        icon_img = custom_icon if custom_icon else self.extract_high_res_icon(exe_path, 0, THEME["body_bg"], self.icon_size)
        icon_lbl = tk.Label(row, image=icon_img, bg=THEME["body_bg"], cursor="hand2")
        icon_lbl.pack(side="left", padx=(0, 14))

        status_lbl = tk.Label(row, bg=THEME["body_bg"], width=4, anchor="e", cursor="hand2")
        status_lbl.pack(side="right")

        slider = ModernSlider(
            row, self, value=val, muted=(is_muted or val == 0), bg_color=THEME["body_bg"],
            accent_color=THEME["accent"], width=int(320 * self.dpi_scale), height=self.slider_height
        )
        slider.pack(side="left", fill="x", expand=True)

        def update_right_indicator(muted_state, current_v):
            if current_v > 0:
                row_state["last_nonzero"] = current_v
            is_silent = muted_state or (current_v == 0)
            if is_silent:
                status_lbl.config(image=self.app_red_mute_icon, text="", width=self.mute_icon_size + 4)
            else:
                status_lbl.config(image="", text=str(current_v), fg=THEME["text_main"], font=("Segoe UI", 13), width=4)
            slider.set_state(current_v, is_silent)

        update_right_indicator(is_muted, val)

        def on_app_slide(v):
            for s in app_sessions:
                try:
                    ctl = s._ctl.QueryInterface(ISimpleAudioVolume)
                    ctl.SetMasterVolume(v / 100.0, None)
                    if v == 0:
                        ctl.SetMute(1, None)
                    elif ctl.GetMute():
                        ctl.SetMute(0, None)
                except Exception:
                    logging.debug("Session volume change failed for %s", exe_path, exc_info=True)
            update_right_indicator(v == 0, v)

        def on_app_mute(e):
            try:
                first_c = app_sessions[0]._ctl.QueryInterface(ISimpleAudioVolume)
                cur_m = bool(first_c.GetMute())
                cur_v = int(round(first_c.GetMasterVolume() * 100))

                if cur_m or cur_v == 0:
                    restore_v = row_state["last_nonzero"] if cur_v == 0 else cur_v
                    for s in app_sessions:
                        c = s._ctl.QueryInterface(ISimpleAudioVolume)
                        c.SetMasterVolume(restore_v / 100.0, None)
                        c.SetMute(0, None)
                    update_right_indicator(False, restore_v)
                else:
                    for s in app_sessions:
                        s._ctl.QueryInterface(ISimpleAudioVolume).SetMute(1, None)
                    update_right_indicator(True, cur_v)
            except Exception:
                logging.warning("Failed to toggle app mute for %s", exe_path, exc_info=True)

        slider.command = on_app_slide
        icon_lbl.bind("<Button-1>", on_app_mute)
        status_lbl.bind("<Button-1>", on_app_mute)

        self.app_sync_refs.append({"sessions": app_sessions, "update_ui": update_right_indicator})

    def toggle_device_dropdown(self):
        self.device_list_expanded = not self.device_list_expanded
        if self.device_list_expanded:
            self.settings_expanded = False
        self.build_flyout_ui()
        self.position_flyout()

    def toggle_inline_settings(self):
        self.settings_expanded = not self.settings_expanded
        if self.settings_expanded:
            self.device_list_expanded = False
        else:
            self.recording_target = None
            self.reload_hotkeys()
        self.build_flyout_ui()
        self.position_flyout()

    def select_device_from_list(self, dev_id):
        self.device_list_expanded = False
        self.set_default_device(dev_id, show_osd=False)

    # -----------------------------------------------------------------
    # Multi-Monitor Flyout Positioning & Max-Height Cap
    # -----------------------------------------------------------------
    def position_flyout(self):
        self.root.update_idletasks()
        work_left, work_top, work_right, work_bottom = (
            self._target_monitor_work if self._target_monitor_work else self.get_active_monitor_work_area()
        )
        screen_avail_h = max(400, work_bottom - work_top - 28)
        max_flyout_h = int(screen_avail_h * 0.82)

        header_req_h = self.header_frame.winfo_reqheight() if hasattr(self, "header_frame") else 140
        apps_req_h = self.apps_frame.winfo_reqheight() if self.apps_frame else 100

        max_body_h = max(int(120 * self.dpi_scale), max_flyout_h - header_req_h)
        actual_body_h = min(apps_req_h, max_body_h)

        if self.body_canvas:
            self.body_canvas.configure(height=actual_body_h, scrollregion=(0, 0, self.flyout_width - 2, apps_req_h))

        w = self.flyout_width
        h = header_req_h + actual_body_h + 2

        x = work_right - w - 14
        y = work_bottom - h - 14
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def show_flyout(self, expand_settings=False):
        self.close_tray_menu()
        self._master_vol = None
        self._target_monitor_work = self.get_active_monitor_work_area()
        self.device_list_expanded = False
        self.settings_expanded = expand_settings
        self.recording_target = None
        self.build_flyout_ui()
        self.position_flyout()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._cached_hwnd = self.get_hwnd(self.root)
        self.is_visible = True
        self.had_focus = False
        self.open_timestamp = time.time()
        self.start_active_watchers()

    def hide_flyout(self):
        if self.is_visible:
            self.last_hide_ts = time.time()
            self.stop_active_watchers()
            if self.recording_target:
                self.recording_target = None
                self.reload_hotkeys()
            self.root.withdraw()
            self.is_visible = False

    def toggle_flyout(self, from_tray=False):
        self.close_tray_menu()
        if from_tray and not self.is_visible and (time.time() - self.last_hide_ts) < 0.35:
            return
        if self.is_visible:
            self.hide_flyout()
        else:
            self.show_flyout()

    # -----------------------------------------------------------------
    # System Tray Icon (Simplified High-Contrast Symbol) & Clean Shutdown
    # -----------------------------------------------------------------
    def start_tray_icon(self):
        # Fallback menu in case OS forces standard menu
        fallback_menu = pystray.Menu(
            pystray.MenuItem("Open Mixer", lambda icon, item: self.post(self.toggle_flyout, True), default=True),
            pystray.MenuItem("Switch Audio Device", lambda icon, item: self.post(self.cycle_audio_device, True)),
            pystray.MenuItem("Settings", lambda icon, item: self.post(self.show_flyout, True)),
            pystray.MenuItem(f"About ({APP_VERSION})", lambda icon, item: self.post(self.show_about_dialog)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self.quit_app)
        )
        tray_img = create_brand_logo_pil(size=64, simplified_tray=True)
        self.tray = AudioHubTrayIcon(self, "AudioHub", tray_img, f"AudioHub ({APP_VERSION})", fallback_menu)
        threading.Thread(target=self.tray.run, name="TrayThread", daemon=True).start()

    def quit_app(self, icon=None, item=None):
        if self.hk_thread_id:
            user32.PostThreadMessageW(self.hk_thread_id, WM_APP_QUIT_HOTKEYS, 0, 0)
        if self.tray:
            self.tray.stop()
        self.post(self._shutdown)

    def _shutdown(self):
        self._quitting = True
        self.close_tray_menu()
        self.stop_active_watchers()
        self.root.quit()

    def run(self):
        self.root.mainloop()
        try:
            self.root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    ERROR_ALREADY_EXISTS = 183
    MB_ICONINFORMATION = 0x00000040
    ctypes.set_last_error(0)
    mutex_handle = kernel32.CreateMutexW(None, False, "Local\\AudioHub_SingleInstance_Mutex_v1")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        user32.MessageBoxW(0, "AudioHub is already running (check the system tray).", "AudioHub", MB_ICONINFORMATION)
        sys.exit(0)

    app = AudioHubFlyout()
    app.run()
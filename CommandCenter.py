import os
import re
import json
import threading
import shutil
import subprocess
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import customtkinter as ctk

# V130 render-performance patch: CustomTkinter 5.2.2 calls
# update_idletasks() once for EVERY CTk widget during an appearance-mode
# change. With a dashboard containing many widgets this makes a single
# Light/Dark switch noticeably slow. Keep the per-widget redraw, but flush
# Tk's idle queue only once from the application-level toggle handler.
from customtkinter.windows.widgets.core_widget_classes.ctk_base_class import CTkBaseClass
from customtkinter.windows.widgets.appearance_mode.appearance_mode_base_class import CTkAppearanceModeBaseClass
from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import AppearanceModeTracker

def _fast_ctk_set_appearance_mode(self, mode_string):
    # Update the widget appearance state and redraw without CustomTkinter's
    # per-widget update_idletasks() call.
    CTkAppearanceModeBaseClass._set_appearance_mode(self, mode_string)
    self._draw()

CTkBaseClass._set_appearance_mode = _fast_ctk_set_appearance_mode

# V138: fast appearance repaint.
# CustomTkinter redraws the geometry of every canvas widget during an appearance
# change even though the geometry does not change. On Windows this can make the
# native window spend seconds rebuilding identical rounded rectangles, which
# exposes a black/grey intermediate frame. During a theme switch we therefore
# skip geometry generation and let the existing canvas items receive only their
# new colors. Normal drawing is restored immediately after the switch.
_THEME_REPAINT_FAST = False

from customtkinter.windows.widgets.core_rendering.draw_engine import DrawEngine

_draw_engine_methods = (
    "draw_rounded_rect_with_border",
    "draw_rounded_rect_with_border_vertical_split",
    "draw_rounded_progress_bar_with_border",
    "draw_rounded_scrollbar",
    "draw_rounded_slider_with_border_and_button",
    "draw_background_corners",
    "draw_checkmark",
    "draw_dropdown_arrow",
)

_original_draw_engine_methods = {}
for _name in _draw_engine_methods:
    _original_draw_engine_methods[_name] = getattr(DrawEngine, _name)

    def _make_fast_draw(original):
        def _fast_draw(self, *args, **kwargs):
            if _THEME_REPAINT_FAST:
                return False
            return original(self, *args, **kwargs)
        return _fast_draw

    setattr(DrawEngine, _name, _make_fast_draw(_original_draw_engine_methods[_name]))

_original_tracker_update_callbacks = AppearanceModeTracker.update_callbacks

@classmethod
def _fast_update_callbacks(cls):
    global _THEME_REPAINT_FAST
    mode = "Light" if cls.appearance_mode == 0 else "Dark"
    callbacks = list(cls.callback_list)

    _THEME_REPAINT_FAST = True
    try:
        # Appearance callbacks only need to recolor already-created canvas
        # objects. Widget geometry is unchanged by light/dark mode.
        for callback in callbacks:
            try:
                callback(mode)
            except Exception:
                pass
    finally:
        _THEME_REPAINT_FAST = False

AppearanceModeTracker.update_callbacks = _fast_update_callbacks

# Theme is controlled by the Dark Mode switch.  CustomTkinter accepts
# (light, dark) color tuples and automatically updates every CTk widget.
ctk.set_default_color_theme("dark-blue")

THEME = {
    "bg": ("#D9DDE3", "#0B1118"),
    "card_bg": ("#ECEFF2", "#121A24"),
    "card_border": ("#D0D7DE", "#213244"),
    "accent_green": ("#1A7F37", "#2EA043"),
    "accent_green_bg": ("#DAFBE1", "#10381B"),
    "accent_red": ("#CF222E", "#F85149"),
    "accent_red_bg": ("#FFEBE9", "#490202"),
    "accent_blue": ("#0969DA", "#1F6FEB"),
    "accent_blue_hover": ("#54AEFF", "#388BFD"),
    "accent_amber": ("#9A6700", "#D29922"),
    "text_primary": ("#1F2328", "#E6EDF3"),
    "text_muted": ("#656D76", "#8B949E"),
    "console_bg": ("#E3E7EB", "#010409"),
    "btn_neutral": ("#0969DA", "#16202C"),
    "btn_neutral_hover": ("#54AEFF", "#223142"),
}

def _theme_color(key):
    """Return the color for the currently selected appearance mode."""
    value = THEME[key]
    if isinstance(value, tuple):
        return value[1] if ctk.get_appearance_mode().lower() == "dark" else value[0]
    return value

WIN_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(APP_DIR, "commands.json")

_USER_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", APP_DIR), "Command Centre")
try:
    os.makedirs(_USER_CONFIG_DIR, exist_ok=True)
    SETTINGS_FILE = os.path.join(_USER_CONFIG_DIR, "settings.json")
except Exception:
    SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

DEFAULT_COMMANDS = [
    {"name": "ADB Devices", "category": "ADB", "type": "CMD Command", "target": "Laptop", "command": "adb devices"},
    {"name": "ADB Reboot", "category": "ADB", "type": "ADB Command", "target": "Selected DUT(s)", "command": "adb reboot"},
    {"name": "Check ADB Version", "category": "ADB", "type": "CMD Command", "target": "Laptop", "command": "adb version"},
    {"name": "Get Device Model", "category": "ADB", "type": "ADB Command", "target": "Selected DUT(s)", "command": "adb shell getprop ro.product.model"},
    {"name": "Start Scrcpy", "category": "Scrcpy", "type": "Scrcpy Command", "target": "Selected DUT(s)", "command": "scrcpy -s {SERIAL}"},
    {"name": "Enable Thunderbird", "category": "Logs", "type": "Batch Script", "target": "Selected DUT(s)", "command": "adb shell setprop persist.log.tag.Thunderbird VERBOSE\nadb logcat -c"},
]

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --- Drive Detection & Multi-threaded Search Utilities ---

def fixed_drives():
    drives = []
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object -ExpandProperty DeviceID"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
        )
        for line in ps.stdout.splitlines():
            x = line.strip()
            if re.fullmatch(r"[A-Z]:", x.upper()):
                drives.append(x.upper() + "\\")
    except Exception:
        pass
    if not drives:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root_drive = letter + ":\\"
            if os.path.exists(root_drive):
                drives.append(root_drive)
    return list(dict.fromkeys(drives)) or ["C:\\"]

def normalize_exe(value):
    if not value:
        return ""
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"'))))
    return p if os.path.isfile(p) and p.lower().endswith(".exe") else ""

def normalize_jar(value):
    if not value:
        return ""
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"'))))
    return p if os.path.isfile(p) and p.lower().endswith(".jar") else ""

def normalize_scrcpy(value):
    p = normalize_exe(value)
    if p and os.path.basename(p).lower() == "scrcpy.exe":
        return p
    if value and os.path.isdir(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"')))):
        q = os.path.join(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"'))), "scrcpy.exe")
        if os.path.isfile(q):
            return os.path.abspath(q)
    return ""

def detect_scrcpy_fast(saved_path=""):
    p = normalize_scrcpy(saved_path)
    if p:
        return p
    try:
        p = normalize_scrcpy(shutil.which("scrcpy"))
        if p:
            return p
    except Exception:
        pass
    candidates = [
        os.path.join(APP_DIR, "scrcpy.exe"),
        os.path.join(APP_DIR, "scrcpy", "scrcpy.exe"),
        os.path.join(os.path.dirname(APP_DIR), "scrcpy", "scrcpy.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Downloads", "scrcpy", "scrcpy.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "scrcpy", "scrcpy.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "scrcpy", "scrcpy.exe"),
    ]
    for c in candidates:
        p = normalize_scrcpy(c)
        if p:
            return p
    return ""


class ModernDUTCard(ctk.CTkFrame):
    """Compact DUT telemetry card matching the reference dashboard layout."""
    def __init__(self, master, slot_index, on_check_toggle=None, **kwargs):
        super().__init__(
            master,
            fg_color=THEME["card_bg"],
            border_color=THEME["card_border"],
            border_width=1,
            corner_radius=4,
            **kwargs
        )
        self.slot_index = slot_index
        self.grid_columnconfigure(2, weight=1)
        # Fixed card geometry prevents CustomTkinter/DPI scaling from clipping
        # the fifth telemetry row (Temperature). All five fields stay inside
        # every DUT card at the dashboard's compact height.
        self.configure(height=165)
        self.grid_propagate(False)
        self.grid_rowconfigure(0, minsize=27, weight=0)
        for _r in range(1, 5):
            self.grid_rowconfigure(_r, minsize=21, weight=0)
        self.grid_rowconfigure(5, minsize=26, weight=0)

        self.selected_var = ctk.BooleanVar(value=False)
        self.chk = ctk.CTkCheckBox(
            self, text=f"DUT #{slot_index + 1}", variable=self.selected_var,
            command=on_check_toggle, width=16, height=16, corner_radius=2,
            border_width=1, fg_color=THEME["accent_blue"],
            border_color=THEME["card_border"], font=("Segoe UI", 9, "bold"),
            text_color=THEME["text_primary"]
        )
        self.chk.grid(row=0, column=0, columnspan=2, padx=(8, 4), pady=(5, 2), sticky="w")

        self.status_badge = ctk.CTkLabel(
            self, text="DISCONNECTED", font=("Segoe UI", 8, "bold"),
            fg_color=THEME["accent_red_bg"], text_color=THEME["accent_red"],
            corner_radius=5, padx=7, pady=2
        )
        self.status_badge.grid(row=0, column=2, padx=(4, 7), pady=(5, 2), sticky="e")

        self.fields = {}
        for r, key in enumerate(["Serial", "Status", "Model", "Operator"], start=1):
            lbl = ctk.CTkLabel(
                self, text=f"{key}:", font=("Segoe UI", 8, "bold"),
                text_color=THEME["text_muted"]
            )
            lbl.grid(row=r, column=0, sticky="w", padx=(8, 2), pady=0)
            val = ctk.CTkLabel(
                self, text="DISCONNECTED" if key == "Status" else "-",
                font=("Segoe UI", 8),
                text_color=THEME["accent_red"] if key == "Status" else THEME["text_primary"],
                anchor="w"
            )
            val.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(2, 7), pady=0)
            self.fields[key] = val

        temp_box = ctk.CTkFrame(self, fg_color="transparent")
        temp_box.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 1))
        temp_box.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            temp_box, text="Temperature:", font=("Segoe UI", 8, "bold"),
            text_color=THEME["text_muted"]
        ).grid(row=0, column=0, sticky="w")

        self.temp_lbl = ctk.CTkLabel(
            temp_box, text="-- °C", font=("Segoe UI", 8),
            text_color=THEME["text_muted"], anchor="w"
        )
        self.temp_lbl.grid(row=0, column=1, sticky="w", padx=(6, 0))

        self.temp_bar = ctk.CTkProgressBar(
            temp_box, height=4, progress_color=THEME["accent_green"],
            fg_color=THEME["console_bg"]
        )
        self.temp_bar.set(0.0)
        self.temp_bar.grid(row=0, column=2, sticky="ew", padx=(8, 0))

    def update_status(self, serial, model, operator, temp_str, is_connected=False):
        if is_connected:
            self.status_badge.configure(
                text="CONNECTED", fg_color=THEME["accent_green_bg"],
                text_color=THEME["accent_green"]
            )
            self.fields["Serial"].configure(text=serial or "--")
            self.fields["Status"].configure(text="CONNECTED", text_color=THEME["accent_green"])
            self.fields["Model"].configure(text=model or "--")
            self.fields["Operator"].configure(text=operator or "--")
            try:
                temp_val = float(str(temp_str).replace("°C", "").strip())
                self.temp_bar.set(min(1.0, max(0.0, temp_val / 60.0)))
                if temp_val > 45:
                    self.temp_lbl.configure(text=f"{temp_val:.1f} °C", text_color=THEME["accent_red"])
                    self.temp_bar.configure(progress_color=THEME["accent_red"])
                else:
                    self.temp_lbl.configure(text=f"{temp_val:.1f} °C", text_color=THEME["accent_green"])
                    self.temp_bar.configure(progress_color=THEME["accent_green"])
            except Exception:
                self.temp_lbl.configure(text=str(temp_str), text_color=THEME["text_primary"])
        else:
            self.status_badge.configure(
                text="DISCONNECTED", fg_color=THEME["accent_red_bg"],
                text_color=THEME["accent_red"]
            )
            self.fields["Serial"].configure(text="-")
            self.fields["Status"].configure(text="DISCONNECTED", text_color=THEME["accent_red"])
            self.fields["Model"].configure(text="-")
            self.fields["Operator"].configure(text="-")
            self.temp_lbl.configure(text="-- °C", text_color=THEME["text_muted"])
            self.temp_bar.set(0.0)


class CommandCenterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("COMMAND CENTRE")
        self.geometry("1450x850")
        self.minsize(1180, 720)
        self.configure(fg_color=THEME["bg"])

        self.commands_data = load_json(DATA_FILE, DEFAULT_COMMANDS.copy())
        self.settings = load_json(SETTINGS_FILE, {
            "scrcpy_path": "", "nlt_path": "", "mre_nexus_path": "",
            "shannon_dm_path": "", "self_tool_path": "", "go_batch_path": "",
            "dark_mode": True
        })
        self.dark_mode = bool(self.settings.get("dark_mode", True))
        ctk.set_appearance_mode("dark" if self.dark_mode else "light")

        self.devices = []
        self.connection_state = [False] * 4
        self._device_details_cache = {}
        self._device_details_inflight = set()
        self.active_processes = set()
        self.active_processes_lock = threading.Lock()
        self.scrcpy_processes = {}
        self.scrcpy_launching = set()
        self.scrcpy_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.output_window = None
        self.apk_var = tk.StringVar(value="Not selected")
        self.gate_ts_var = tk.StringVar(value="Not selected")

        self.status_var = tk.StringVar(value="0 device(s) detected.")
        self.category_var = tk.StringVar(value="All")

        # Top-level layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_top_header()
        self._build_upper_tab_selector()
        self._build_main_views()

        # Do not force a synchronous Tk paint here.  The first natural mainloop
        # frame should paint the dashboard immediately; an explicit update()
        # blocks startup while Tk lays out/redraws every widget again.
        # Background work remains deferred so ADB/telemetry cannot block startup.
        self.after(900, self.refresh_devices)
        self.after(1500, self._fast_connection_watch)
        self.after(3000, self._live_temperature_watch)

    def _build_top_header(self):
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=14, pady=(8, 2), sticky="ew")
        header_frame.grid_columnconfigure(1, weight=1)
        header_frame.grid_columnconfigure(3, weight=0)

        title_lbl = ctk.CTkLabel(
            header_frame, text="COMMAND CENTRE",
            font=("Segoe UI", 16, "bold"), text_color=THEME["text_primary"]
        )
        title_lbl.grid(row=0, column=0, sticky="w")

        sub_lbl = ctk.CTkLabel(
            header_frame, text="DUT automation • command execution • live device monitoring",
            font=("Segoe UI", 10), text_color=THEME["accent_blue"]
        )
        sub_lbl.grid(row=1, column=0, sticky="w", pady=(1, 0))

        credit_lbl = ctk.CTkLabel(
            header_frame, text="Created by Khushnoor",
            font=("Segoe UI", 9, "italic"), text_color=THEME["text_muted"]
        )
        credit_lbl.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, 12))

        self.dark_mode_var = ctk.BooleanVar(value=self.dark_mode)
        theme_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        theme_box.grid(row=0, column=3, rowspan=2, sticky="e")
        self.theme_mode_label = ctk.CTkLabel(
            theme_box, text=("Dark Mode" if self.dark_mode else "Light Mode"), font=("Segoe UI", 9),
            text_color=THEME["text_primary"]
        )
        self.theme_mode_label.pack(side="left", padx=(0, 6))

        # Use a small custom track/knob instead of CTkSwitch so the requested
        # track colors are unambiguous on every CustomTkinter appearance mode.
        # Both modes use a BLUE track (#1F6FEB). The knob moves left/right.
        self.theme_switch = ctk.CTkFrame(
            theme_box, width=42, height=22, corner_radius=11,
            fg_color="#1F6FEB"
        )
        self.theme_switch.pack(side="left")
        self.theme_switch.pack_propagate(False)
        self.theme_switch.grid_propagate(False)

        self.theme_switch_knob = ctk.CTkFrame(
            self.theme_switch, width=16, height=16, corner_radius=8,
            fg_color="#FFFFFF"
        )
        self.theme_switch_knob.place(
            x=23 if self.dark_mode else 3, y=3
        )

        for widget in (self.theme_switch, self.theme_switch_knob):
            widget.bind("<Button-1>", self._on_theme_switch_click)
            widget.bind("<Enter>", lambda _e: self.theme_switch.configure(
                fg_color="#2B7FFF"
            ))
            widget.bind("<Leave>", lambda _e: self._refresh_theme_switch())

    def _refresh_theme_switch(self):
        """Keep the visible track blue in both modes and move the knob left/right."""
        try:
            enabled = bool(self.dark_mode_var.get())
            if hasattr(self, "theme_mode_label"):
                self.theme_mode_label.configure(text="Dark Mode" if enabled else "Light Mode", text_color=THEME["text_primary"])
            self.theme_switch.configure(
                fg_color="#1F6FEB"
            )
            self.theme_switch_knob.place(
                x=23 if enabled else 3, y=3
            )
            self.theme_switch_knob.configure(fg_color="#FFFFFF")
        except Exception:
            pass

    def _on_theme_switch_click(self, _event=None):
        self.dark_mode_var.set(not bool(self.dark_mode_var.get()))
        self._toggle_theme()

    def _toggle_theme(self):
        """Switch theme using color-only repaint of existing widget geometry."""
        self.dark_mode = bool(self.dark_mode_var.get())
        self.settings["dark_mode"] = self.dark_mode
        self._theme_switch_in_progress = True

        # Do not call update()/update_idletasks() and do not disable WM redraw.
        # Let the normal event loop paint each small callback batch.
        ctk.set_appearance_mode("dark" if self.dark_mode else "light")
        self._refresh_theme_switch()

        if getattr(self, "_theme_finish_job", None):
            try:
                self.after_cancel(self._theme_finish_job)
            except Exception:
                pass
        self._theme_finish_job = self.after(0, self._finish_theme_change)

    def _set_window_redraw(self, enabled):
        # Kept as a compatibility no-op. V135's WM_SETREDRAW suppression caused
        # an unnecessary black intermediate frame during a large repaint.
        return

    def _finish_theme_change(self):
        self._theme_finish_job = None
        try:
            save_json(SETTINGS_FILE, self.settings)
        except Exception:
            pass
        self._refresh_native_theme_widgets()
        self._refresh_theme_switch()
        self._theme_switch_in_progress = False

    def _refresh_native_theme_widgets(self):
        """Refresh native Tk/ttk pieces that do not consume CTk color tuples."""
        try:
            style = ttk.Style()
            style.configure(
                "Treeview", background=_theme_color("card_bg"), foreground=_theme_color("text_primary"),
                fieldbackground=_theme_color("card_bg"), rowheight=16
            )
            style.configure(
                "Treeview.Heading", background=_theme_color("btn_neutral"),
                foreground=_theme_color("text_primary")
            )
            style.map("Treeview", background=[("selected", _theme_color("accent_blue_hover"))], foreground=[("selected", "#FFFFFF")])
        except Exception:
            pass
        for widget_name in ("tree_frame",):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                try:
                    widget.configure(bg=_theme_color("console_bg"))
                except Exception:
                    pass
        try:
            if hasattr(self, "console"):
                self.console.tag_config("error", foreground=_theme_color("accent_red"))
        except Exception:
            pass

    def _build_upper_tab_selector(self):
        """Builds the upper tab selector pills directly below the header."""
        tab_bar = ctk.CTkFrame(self, fg_color="transparent")
        tab_bar.grid(row=1, column=0, padx=14, pady=(6, 4), sticky="w")

        self.btn_dashboard_tab = ctk.CTkButton(
            tab_bar, text="⌂ Dashboard", font=("Segoe UI", 10, "bold"),
            width=96, height=26, corner_radius=4,
            fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"],
            command=self.show_dashboard_tab
        )
        self.btn_dashboard_tab.pack(side="left", padx=(0, 6))

        self.btn_pctools_tab = ctk.CTkButton(
            tab_bar, text="🛠 PC Tools", font=("Segoe UI", 10, "bold"),
            width=96, height=26, corner_radius=4,
            fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"],
            border_width=1, border_color=THEME["card_border"],
            command=self.show_pctools_tab
        )
        self.btn_pctools_tab.pack(side="left")

    def show_dashboard_tab(self):
        self.btn_dashboard_tab.configure(fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"], border_width=0, text_color=("#FFFFFF", "#E6EDF3"))
        self.btn_pctools_tab.configure(fg_color=THEME["btn_neutral"], border_width=1, border_color=THEME["card_border"])
        self.pc_tools_view.pack_forget()
        self.dashboard_view.pack(fill="both", expand=True, padx=14, pady=(0, 6))

    def show_pctools_tab(self):
        self.btn_pctools_tab.configure(fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"], border_width=0, text_color="#FFFFFF")
        self.btn_dashboard_tab.configure(fg_color=THEME["btn_neutral"], border_width=1, border_color=THEME["card_border"])
        self.dashboard_view.pack_forget()
        if not self._pc_tools_built:
            self._build_pc_tools_content(self.pc_tools_view)
            self._pc_tools_built = True
        self.pc_tools_view.pack(fill="both", expand=True, padx=14, pady=(0, 6))

    def _build_main_views(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.grid(row=2, column=0, sticky="nsew")

        self.dashboard_view = ctk.CTkFrame(container, fg_color="transparent")
        self.pc_tools_view = ctk.CTkFrame(container, fg_color="transparent")
        self._pc_tools_built = False

        # Build only the dashboard on startup.  PC Tools is lazy-built the first
        # time its tab is opened; this removes a large amount of unnecessary
        # widget construction from the critical startup/render path.
        self._build_dashboard_content(self.dashboard_view)

        # Default visible tab
        self.dashboard_view.pack(fill="both", expand=True, padx=14, pady=(0, 6))

    def _build_dashboard_content(self, parent):
        # Reference layout: left dashboard workspace + right Quick Features column.
        parent.grid_rowconfigure(0, weight=0)
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=3, uniform="dashboard")
        parent.grid_columnconfigure(1, weight=1, uniform="dashboard")

        left = ctk.CTkFrame(parent, fg_color="transparent")
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 5))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        # --- DUT group: one blue outer border around all four DUT cards ---
        dut_section = ctk.CTkFrame(
            left, fg_color=THEME["card_bg"], border_color=THEME["accent_blue"],
            border_width=1, corner_radius=4, height=215
        )
        dut_section.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        dut_section.grid_propagate(False)
        dut_section.grid_columnconfigure(0, weight=1)
        dut_section.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            dut_section, text="Connected Devices",
            font=("Segoe UI", 9, "bold"), text_color=THEME["text_primary"]
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(4, 2))

        cards_row = ctk.CTkFrame(dut_section, fg_color="transparent")
        cards_row.grid(row=1, column=0, sticky="nsew", padx=7, pady=(0, 5))
        cards_row.grid_rowconfigure(0, minsize=165, weight=0)
        for i in range(4):
            cards_row.grid_columnconfigure(i, weight=1, uniform="dut_cards")

        self.dut_cards = []
        for i in range(4):
            card = ModernDUTCard(cards_row, slot_index=i, height=165, on_check_toggle=self._on_dut_check_toggle)
            card.grid(row=0, column=i, padx=3, pady=(0, 0), sticky="ew")
            self.dut_cards.append(card)

        # --- Lower left workspace ---
        workspace = ctk.CTkFrame(left, fg_color="transparent")
        workspace.grid(row=1, column=0, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(0, weight=0)  # filter
        workspace.grid_rowconfigure(1, weight=0)  # command library
        workspace.grid_rowconfigure(2, weight=0)  # action bar
        workspace.grid_rowconfigure(3, weight=1)  # console
        workspace.grid_rowconfigure(4, weight=0)  # status

        filter_bar = ctk.CTkFrame(
            workspace, fg_color=THEME["card_bg"], corner_radius=4,
            border_width=1, border_color=THEME["card_border"], height=31
        )
        filter_bar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        filter_bar.grid_propagate(False)
        ctk.CTkLabel(filter_bar, text="Search:", font=("Segoe UI", 8, "bold"), text_color=THEME["text_muted"]).pack(side="left", padx=(8, 3))
        self.search_entry = ctk.CTkEntry(
            filter_bar, height=22, width=128, font=("Segoe UI", 8),
            fg_color=THEME["console_bg"], border_color=THEME["card_border"]
        )
        self.search_entry.pack(side="left", padx=3)
        self.search_entry.bind("<KeyRelease>", lambda e: self.refresh_command_tree())
        ctk.CTkLabel(filter_bar, text="Category:", font=("Segoe UI", 8, "bold"), text_color=THEME["text_muted"]).pack(side="left", padx=(8, 3))
        self.category_dropdown = ctk.CTkComboBox(
            filter_bar, height=22, width=110, values=["All"], variable=self.category_var,
            command=lambda e: self.refresh_command_tree(), fg_color=THEME["console_bg"],
            border_color=THEME["card_border"], font=("Segoe UI", 8)
        )
        self.category_dropdown.pack(side="left", padx=3)

        cmd_wrapper = ctk.CTkFrame(
            workspace, fg_color=THEME["card_bg"], corner_radius=4,
            border_width=1, border_color=THEME["card_border"], height=126
        )
        cmd_wrapper.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        cmd_wrapper.grid_propagate(False)
        ctk.CTkLabel(
            cmd_wrapper,
            text="Command Library",
            font=("Segoe UI", 8), text_color=THEME["text_muted"], justify="left"
        ).pack(anchor="w", padx=8, pady=(5, 3))

        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Treeview", background=_theme_color("card_bg"), foreground=_theme_color("text_primary"),
            rowheight=16, fieldbackground=_theme_color("card_bg"), borderwidth=0,
            font=("Segoe UI", 8)
        )
        style.configure(
            "Treeview.Heading", background=_theme_color("btn_neutral"), foreground=_theme_color("text_primary"),
            relief="flat", font=("Segoe UI", 8, "bold")
        )
        style.map("Treeview", background=[("selected", _theme_color("accent_blue_hover"))], foreground=[("selected", "#ffffff")])

        self.tree_frame = tk.Frame(cmd_wrapper, bg=_theme_color("console_bg"), bd=0, highlightthickness=0)
        tree_frame = self.tree_frame
        tree_frame.pack(fill="both", expand=True, padx=7, pady=(0, 6))
        self.tree = ttk.Treeview(tree_frame, columns=("Category", "Type", "Target"), selectmode="extended", height=6, show="tree headings")
        self.tree.heading("#0", text="Command Name")
        self.tree.heading("Category", text="Category")
        self.tree.heading("Type", text="Type")
        self.tree.heading("Target", text="Target")
        self.tree.column("#0", width=260, minwidth=160, stretch=True)
        self.tree.column("Category", width=95, minwidth=70, stretch=True)
        self.tree.column("Type", width=130, minwidth=90, stretch=True)
        self.tree.column("Target", width=130, minwidth=100, stretch=True)
        # V122 dark scrollbar: use CustomTkinter instead of the old native Tk/ttk scrollbar.
        vs = ctk.CTkScrollbar(
            tree_frame, orientation="vertical", width=10,
            fg_color=THEME["console_bg"],
            button_color=THEME["card_border"],
            button_hover_color=THEME["accent_blue"]
        )
        vs.configure(command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns", padx=(3, 1))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self.run_selected_commands())

        action_bar = ctk.CTkFrame(workspace, fg_color="transparent", height=30)
        action_bar.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        action_bar.grid_propagate(False)
        # Compact action row: fixed-width buttons stay grouped on the left,
        # leaving intentional blank space on the right like the V122 reference design.
        buttons = [
            ("▶ RUN", 92, THEME["accent_green"], "#238636", self.run_selected_commands),
            ("■ STOP", 80, THEME["accent_red"], "#DA3633", self.stop_execution),
            ("Scrcpy", 84, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.start_scrcpy_button),
            ("APK Install", 92, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.start_apk_install_flow),
            ("Gate TS", 84, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.start_gate_ts_flow),
            ("Copy", 84, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.copy_selected),
            ("Export", 84, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.export_commands),
            ("Import", 84, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.import_commands),
            ("Pull Logs", 92, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.open_go_batch),
            ("↗ Detach", 90, THEME["btn_neutral"], THEME["btn_neutral_hover"], self.open_output_window),
        ]
        for text, width, fg, hover, fn in buttons:
            ctk.CTkButton(
                action_bar, text=text, width=width, fg_color=fg, hover_color=hover,
                border_width=0 if fg in (THEME["accent_green"], THEME["accent_red"]) else 1,
                border_color=THEME["card_border"],
                font=("Segoe UI", 8, "bold" if fg != THEME["btn_neutral"] else "normal"),
                height=26, command=fn
            ).pack(side="left", fill="none", expand=False, padx=(0, 4))

        console_frame = ctk.CTkFrame(
            workspace, fg_color=THEME["console_bg"], corner_radius=4,
            border_width=1, border_color=THEME["card_border"]
        )
        console_frame.grid(row=3, column=0, sticky="nsew")
        ctk.CTkLabel(
            console_frame, text="LIVE CONSOLE OUTPUT", font=("Segoe UI", 8, "bold"),
            text_color=THEME["text_muted"]
        ).pack(anchor="w", padx=7, pady=(6, 0))
        self.console = ctk.CTkTextbox(
            console_frame, fg_color="transparent", text_color=THEME["text_primary"],
            font=("Consolas", 9), wrap="none"
        )
        self.console.pack(fill="both", expand=True, padx=5, pady=3)
        try:
            self.console.tag_config("error", foreground=THEME["accent_red"])
        except Exception:
            pass

        bot_bar = ctk.CTkFrame(workspace, fg_color="transparent", height=22)
        bot_bar.grid(row=4, column=0, sticky="ew", pady=(2, 0))
        ctk.CTkLabel(bot_bar, textvariable=self.status_var, font=("Segoe UI", 8), text_color=THEME["text_muted"]).pack(side="left")
        ctk.CTkButton(
            bot_bar, text="Clear Output", width=72, height=20, font=("Segoe UI", 8),
            fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"],
            border_width=1, border_color=THEME["card_border"], text_color=("#FFFFFF", "#E6EDF3"), command=self.clear_output
        ).pack(side="right")

        # --- Right column: DUT management controls above Quick Features ---
        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(5, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        controls = ctk.CTkFrame(right, fg_color="transparent", height=66)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        controls.grid_propagate(False)
        for c in range(3):
            controls.grid_columnconfigure(c, weight=1, uniform="ctrl")
        for c, (txt, fn) in enumerate([("Select All", self.select_all), ("Unselect All", self.unselect_all), ("⟳ Refresh", self.refresh_devices)]):
            ctk.CTkButton(controls, text=txt, height=26, font=("Segoe UI", 8, "bold"), fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"], border_width=1, border_color=THEME["card_border"], text_color=("#FFFFFF", "#E6EDF3"), command=fn).grid(row=0, column=c, padx=2, pady=(0, 3), sticky="ew")
        for c, (txt, fn) in enumerate([("+ Add", self.add_command), ("Edit", self.edit_selected), ("Delete", self.delete_selected)]):
            ctk.CTkButton(controls, text=txt, height=26, font=("Segoe UI", 8, "bold"), fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"], border_width=1, border_color=THEME["card_border"], text_color=("#FFFFFF", "#E6EDF3"), command=fn).grid(row=1, column=c, padx=2, pady=(0, 2), sticky="ew")

        right_panel = ctk.CTkFrame(
            right, fg_color=THEME["card_bg"], corner_radius=4,
            border_width=1, border_color=THEME["card_border"]
        )
        right_panel.grid(row=1, column=0, sticky="nsew")
        ctk.CTkLabel(
            right_panel, text="Quick Features", font=("Segoe UI", 9, "bold"),
            text_color=THEME["accent_blue"]
        ).pack(pady=(7, 2), padx=9, anchor="w")
        self.qf_connected_var = tk.StringVar(value="Connected DUTs: None")
        self.qf_selected_var = tk.StringVar(value="Selected targets: None")
        ctk.CTkLabel(
            right_panel, textvariable=self.qf_connected_var, font=("Segoe UI", 7, "bold"),
            text_color=THEME["text_muted"], anchor="w", justify="left"
        ).pack(fill="x", padx=9, pady=(0, 1))
        ctk.CTkLabel(
            right_panel, textvariable=self.qf_selected_var, font=("Segoe UI", 7),
            text_color=THEME["accent_green"], anchor="w", justify="left"
        ).pack(fill="x", padx=9, pady=(0, 4))
        self.qf_container = ctk.CTkScrollableFrame(right_panel, fg_color="transparent", scrollbar_button_color=THEME["card_border"], scrollbar_button_hover_color=THEME["text_muted"])
        self.qf_container.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.qf_container.grid_columnconfigure(0, weight=1)
        self.qf_container.grid_columnconfigure(1, weight=1)

        self._update_qf_target_display()
        self.refresh_command_tree()

    # --- PC Tools Content & Parallel Search ---

    def _build_pc_tools_content(self, parent):
        panel = ctk.CTkFrame(parent, fg_color=THEME["card_bg"], corner_radius=4, border_width=1, border_color=THEME["card_border"])
        panel.pack(fill="both", expand=True, pady=4)

        ctk.CTkLabel(panel, text="LAPTOP TOOLS", font=("Segoe UI", 14, "bold"), text_color=THEME["text_primary"]).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(panel, text="Search, select version/path, and open tools", font=("Segoe UI", 9), text_color=THEME["text_muted"]).pack(anchor="w", padx=14, pady=(0, 10))

        tools_config = [
            ("NLT", "nlt_path", ["*NLT*.exe", "NLT.exe"]),
            ("MRE Nexus", "mre_nexus_path", ["*MRE*Nexus*.exe", "*MRENexus*.exe", "MRE_Nexus.exe"]),
            ("Shannon DM", "shannon_dm_path", ["*Shannon*DM*.exe", "*Shannon*.exe"]),
            ("Self Tool", "self_tool_path", ["self.jar", "self*.jar"])
        ]

        self.tool_vars = {}
        for name, key, patterns in tools_config:
            row = ctk.CTkFrame(panel, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=4)
            ctk.CTkLabel(row, text=f"{name}:", width=110, font=("Segoe UI", 10, "bold"), anchor="w").pack(side="left")

            var = tk.StringVar(value=self.settings.get(key, "") or "Not configured")
            self.tool_vars[key] = var
            entry = ctk.CTkEntry(row, textvariable=var, font=("Segoe UI", 9), height=24, fg_color=THEME["console_bg"], border_color=THEME["card_border"])
            entry.pack(side="left", fill="x", expand=True, padx=6)

            ctk.CTkButton(row, text="Search PC", width=70, height=24, font=("Segoe UI", 9), fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"],
                          text_color=("#FFFFFF", "#E6EDF3"), command=lambda k=key, v=var, n=name, p=patterns: self.search_tool(k, v, n, p)).pack(side="left", padx=2)
            ctk.CTkButton(row, text="Browse", width=65, height=24, font=("Segoe UI", 9), fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"],
                          text_color=("#FFFFFF", "#E6EDF3"), command=lambda k=key, v=var, n=name: self.browse_generic_tool(k, v, n)).pack(side="left", padx=2)
            ctk.CTkButton(row, text="OPEN", width=65, height=24, font=("Segoe UI", 9, "bold"), fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"],
                          text_color=("#FFFFFF", "#E6EDF3"), command=lambda k=key, n=name: self.open_generic_tool(k, n)).pack(side="left", padx=2)


    def create_search_window(self, title, initial_text):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("950x520")
        win.minsize(760, 420)
        win.transient(self)
        win.configure(fg_color=THEME["card_bg"])

        ctk.CTkLabel(win, text=title, font=("Segoe UI", 12, "bold"), text_color=THEME["text_primary"]).pack(anchor="w", padx=14, pady=(12, 4))
        status_lbl = ctk.CTkLabel(win, text=initial_text, text_color=THEME["text_muted"])
        status_lbl.pack(anchor="w", padx=14, pady=(0, 6))

        out = ctk.CTkTextbox(win, wrap="none", font=("Consolas", 9), height=300, fg_color=THEME["console_bg"], text_color=THEME["text_primary"])
        out.pack(fill="both", expand=True, padx=14, pady=6)

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(0, 12))

        progress = ctk.CTkProgressBar(bar, mode="indeterminate", width=220, progress_color=THEME["accent_blue"])
        progress.pack(side="left")
        progress.start()

        close = ctk.CTkButton(bar, text="Close", command=win.destroy, state="disabled", fg_color=THEME["btn_neutral"], text_color=("#FFFFFF", "#E6EDF3"))
        close.pack(side="right")

        def log(line):
            if not win.winfo_exists():
                return
            out.insert("end", line + "\n")
            out.see("end")

        return win, status_lbl, log, progress, close

    def choose_executable_result(self, title, results, callback):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("900x420")
        win.transient(self)
        win.grab_set()
        win.configure(fg_color=THEME["card_bg"])

        ctk.CTkLabel(win, text=f"Found {len(results)} executable(s). Select the one you want to use:", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=15, pady=12)

        lb = tk.Listbox(win, font=("Consolas", 10), height=13, bg=THEME["console_bg"], fg=THEME["text_primary"],
                        selectbackground=THEME["accent_blue_hover"], selectforeground="#ffffff", borderwidth=1, relief="solid")
        lb.pack(fill="both", expand=True, padx=15)
        for p in results:
            lb.insert("end", p)

        def use_selected():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("Select File", "Please select an executable.", parent=win)
                return
            chosen = results[sel[0]]
            callback(chosen)
            win.destroy()

        lb.bind("<Double-1>", lambda e: use_selected())
        ctk.CTkButton(win, text="Use Selected", command=use_selected, fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"], text_color=("#FFFFFF", "#E6EDF3")).pack(pady=12)

    def search_executables(self, patterns, status_text, callback, display_name="Tool"):
        win, search_status, log, progress, close = self.create_search_window(f"Search PC — {display_name}", status_text)

        def worker():
            results = []
            seen = set()
            lock = threading.Lock()

            def add(path):
                path = normalize_exe(path) if not patterns[0].endswith(".jar") else normalize_jar(path)
                if not path:
                    return
                with lock:
                    if path.lower() not in seen:
                        seen.add(path.lower())
                        results.append(path)

            for pat in patterns:
                if not any(ch in pat for ch in "*?"):
                    try:
                        x = shutil.which(pat)
                        if x:
                            add(x)
                            self.after(0, log, "PATH: " + x)
                    except Exception:
                        pass

            roots = [
                os.path.join(os.environ.get("USERPROFILE", ""), "Downloads"),
                os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
                os.path.join(os.environ.get("USERPROFILE", ""), "Documents"),
                os.path.join(os.environ.get("LOCALAPPDATA", "")),
                os.path.join(os.environ.get("PROGRAMFILES", "")),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""))
            ]
            roots += fixed_drives()
            roots = [r for r in list(dict.fromkeys(roots)) if r and os.path.isdir(r)]

            self.after(0, search_status.configure, {"text": f"Searching {len(roots)} locations in parallel..."})

            def search_root(root_dir):
                self.after(0, log, "Searching: " + root_dir)
                for pat in patterns:
                    try:
                        r = subprocess.run(["where.exe", "/R", root_dir, pat], capture_output=True, text=True,
                                           encoding="utf-8", errors="replace", timeout=45, creationflags=WIN_NO_CONSOLE)
                        for line in r.stdout.splitlines():
                            add(line.strip())
                    except Exception as e:
                        self.after(0, log, f"Skipped/timeout: {root_dir} ({type(e).__name__})")
                self.after(0, log, "Finished: " + root_dir)

            threads = []
            for rd in roots:
                t = threading.Thread(target=search_root, args=(rd,), daemon=True)
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

            results.sort(key=lambda x: x.lower())
            self.after(0, log, f"\nSEARCH COMPLETE — {len(results)} result(s) found.")
            self.after(0, search_status.configure, {"text": f"Search completed — {len(results)} result(s) found."})
            self.after(0, progress.stop)
            self.after(0, close.configure, {"state": "normal"})
            self.after(0, lambda: self._handle_search_results(display_name, results, callback, win))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_search_results(self, display_name, results, callback, search_win):
        if not results:
            messagebox.showwarning(f"{display_name} Not Found", f"No matching files found for {display_name}.\nUse Browse instead.")
            return
        if len(results) == 1:
            callback(results[0])
            if search_win and search_win.winfo_exists():
                search_win.destroy()
            messagebox.showinfo("Tool Found", f"Configured {display_name}:\n\n{results[0]}")
        else:
            if search_win and search_win.winfo_exists():
                search_win.destroy()
            self.choose_executable_result(f"Select {display_name}", results, callback)

    def search_tool(self, key, var, name, patterns):
        def save_selection(path):
            self.settings[key] = path
            save_json(SETTINGS_FILE, self.settings)
            var.set(path)
            self.status_var.set(f"Configured {name}: {path}")
        self.search_executables(patterns, f"Searching laptop for {name}...", save_selection, name)

    # --- Standard Execution & Helpers ---

    def _connected_devices_all(self):
        """Return every currently connected ADB DUT (up to the 4 dashboard slots)."""
        return [(i + 1, d["serial"]) for i, d in enumerate(self.devices[:4]) if d.get("status") == "device"]

    def _target_adb_commands(self, command, serial):
        """Automatically bind every adb invocation in a user command to one DUT.

        This prevents the Windows ADB error 'more than one device/emulator' when
        multiple DUTs are connected. Existing explicit '-s SERIAL' targeting and
        the {SERIAL} placeholder are preserved.
        """
        command = str(command or "").replace("{SERIAL}", serial)
        # Add -s only when an adb invocation does not already specify a serial.
        pattern = r'(?i)(\badb(?:\.exe)?\s+)(?!-s(?:\s|$))'
        return re.sub(pattern, lambda m: m.group(1) + f'-s "{serial}" ', command)

    def _show_install_dialog(self, title, file_path, action_text, command):
        targets = self._connected_devices_all()
        if not targets:
            messagebox.showwarning("No Connected DUTs", "No connected DUTs were detected.")
            return
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("560x210")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        ctk.CTkLabel(win, text=title, font=("Segoe UI", 13, "bold"), text_color=THEME["text_primary"]).pack(anchor="w", padx=16, pady=(14, 5))
        ctk.CTkLabel(win, text=os.path.basename(file_path), font=("Segoe UI", 10, "bold"), text_color=THEME["accent_blue"], anchor="w").pack(fill="x", padx=16)
        ctk.CTkLabel(win, text=f"Connected DUTs: {len(targets)}\n" + "\n".join(f"DUT #{i}: {serial}" for i, serial in targets), font=("Segoe UI", 9), text_color=THEME["text_muted"], justify="left", anchor="w").pack(fill="x", padx=16, pady=(8, 10))
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(bar, text="CANCEL", width=90, fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"], text_color=("#FFFFFF", "#E6EDF3"), command=win.destroy).pack(side="right", padx=(6, 0))
        ctk.CTkButton(bar, text=action_text, width=100, fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"], text_color=("#FFFFFF", "#E6EDF3"), command=lambda: (win.destroy(), command(targets))).pack(side="right")

    def start_apk_install_flow(self):
        f = filedialog.askopenfilename(title="Select APK file", filetypes=[("Android APK", "*.apk")])
        if not f:
            return
        self.apk_var.set(f)
        self._show_install_dialog("APK Install", f, "INSTALL", self.install_apk_on_connected)

    def install_apk_on_connected(self, targets=None):
        apk = self.apk_var.get()
        if not apk or not os.path.isfile(apk) or not apk.lower().endswith(".apk"):
            messagebox.showwarning("APK Missing", "Please select a valid .apk file first.")
            return
        targets = targets or self._connected_devices_all()
        if not targets:
            messagebox.showwarning("No Connected DUTs", "No connected DUTs were detected.")
            return
        self.log(f"\n===== PARALLEL APK INSTALL (ALL CONNECTED DUTS) =====\n> APK: {os.path.basename(apk)}\n> Target DUTs: {len(targets)}")
        def install_worker(serial):
            self.log(f"[{serial}] Installing {os.path.basename(apk)}...")
            try:
                p = subprocess.run(["adb", "-s", serial, "install", "-r", apk], capture_output=True, text=True, creationflags=WIN_NO_CONSOLE)
                if p.returncode == 0 and "Success" in p.stdout:
                    self.log(f"[{serial}] APK Installed Successfully.")
                else:
                    self.log(f"[{serial}] APK Install Failed: {p.stdout.strip() or p.stderr.strip()}")
            except Exception as e:
                self.log(f"[{serial}] APK Install Error: {e}")
        for _, serial in targets:
            threading.Thread(target=install_worker, args=(serial,), daemon=True).start()

    def start_gate_ts_flow(self):
        f = filedialog.askopenfilename(title="Select Gate TS batch file", filetypes=[("Batch Files", "*.bat;*.batch")])
        if not f:
            return
        if not f.lower().endswith((".bat", ".batch")):
            messagebox.showwarning("Invalid Gate TS File", "Please select a .bat or .batch file.")
            return
        self.gate_ts_var.set(f)
        self._show_install_dialog("Gate TS", f, "RUN", self.run_gate_ts_on_connected)

    def run_gate_ts_on_connected(self, targets=None):
        path = self.gate_ts_var.get()
        if not path or not os.path.isfile(path) or not path.lower().endswith((".bat", ".batch")):
            messagebox.showwarning("Gate TS Missing", "Please select a valid .bat or .batch file first.")
            return
        targets = targets or self._connected_devices_all()
        if not targets:
            messagebox.showwarning("No Connected DUTs", "No connected DUTs were detected.")
            return
        self.log(f"\n===== PARALLEL GATE TS RUN (ALL CONNECTED DUTS) =====\n> Script: {os.path.basename(path)}\n> Target DUTs: {len(targets)}")
        def gate_worker(serial):
            self.log(f"[{serial}] Starting Gate TS...")
            try:
                env = os.environ.copy()
                env["DUT_SERIAL"] = serial
                env["SERIAL"] = serial
                cmd = ["cmd.exe", "/d", "/c", "call", path, serial]
                p = subprocess.Popen(cmd, cwd=os.path.dirname(path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=WIN_NO_CONSOLE)
                for line in iter(p.stdout.readline, ""):
                    if line.strip():
                        self.log(f"[{serial}] {line.rstrip()}")
                rc = p.wait()
                self.log(f"[{serial}] Gate TS {'completed successfully' if rc == 0 else f'failed (exit {rc})'}.")
            except Exception as e:
                self.log(f"[{serial}] Gate TS Error: {e}")
        for _, serial in targets:
            threading.Thread(target=gate_worker, args=(serial,), daemon=True).start()

    def log(self, text):
        line = f"{text}\n"
        is_error = bool(re.search(r"\b(failed|failure|error|exception|traceback|could not|not found)\b", str(text), re.IGNORECASE))
        try:
            self.console.insert("end", line, "error" if is_error else "normal")
        except Exception:
            self.console.insert("end", line)
        self.console.see("end")

    def clear_output(self):
        self.console.delete("1.0", "end")

    def _on_dut_check_toggle(self):
        """Refresh the Quick Features target display immediately after a DUT checkbox changes."""
        try:
            self._update_qf_target_display()
        except Exception:
            pass

    def select_all(self):
        for i, card in enumerate(self.dut_cards):
            card.selected_var.set(i < len(self.devices) and self.devices[i].get("status") == "device")
        self._update_qf_target_display()

    def unselect_all(self):
        for card in self.dut_cards:
            card.selected_var.set(False)
        self._update_qf_target_display()

    def selected_devices(self):
        return [
            (i + 1, d["serial"])
            for i, d in enumerate(self.devices[:4])
            if self.dut_cards[i].selected_var.get() and d.get("status") == "device"
        ]

    def _update_qf_target_display(self):
        connected = self._connected_devices_all()
        selected = self.selected_devices()
        if connected:
            self.qf_connected_var.set("Connected DUTs: " + " | ".join(f"DUT #{i}: {serial}" for i, serial in connected))
        else:
            self.qf_connected_var.set("Connected DUTs: None")
        if selected:
            self.qf_selected_var.set("Selected targets: " + " | ".join(f"DUT #{i}: {serial}" for i, serial in selected))
        else:
            self.qf_selected_var.set("Selected targets: None — select a DUT checkbox")

    def refresh_command_tree(self):
        self.tree.delete(*self.tree.get_children())
        search_q = self.search_entry.get().lower().strip()
        cat_filter = self.category_var.get()

        categories = sorted(list(set(c.get("category", "General") for c in self.commands_data)))
        self.category_dropdown.configure(values=["All"] + categories)

        for w in self.qf_container.winfo_children():
            w.destroy()
        self._update_qf_target_display()

        btn_grid_idx = 0
        for idx, cmd in enumerate(self.commands_data):
            name = cmd.get("name", "")
            cat = cmd.get("category", "General")
            typ = cmd.get("type", "CMD Command")
            target = cmd.get("target", "Selected DUT(s)")

            if search_q and search_q not in (name + " " + cmd.get("command", "")).lower():
                continue
            if cat_filter != "All" and cat != cat_filter:
                continue

            self.tree.insert("", "end", iid=str(idx), text=name, values=(cat, typ, target))

            name_lower = name.lower()
            if "block" in name_lower and "unblock" not in name_lower:
                border_color = THEME["accent_red"]
            elif "unblock" in name_lower:
                border_color = THEME["accent_green"]
            elif "log" in name_lower or "pull" in name_lower or "scrcpy" in name_lower:
                border_color = THEME["accent_blue"]
            else:
                border_color = THEME["card_border"]

            btn = ctk.CTkButton(
                self.qf_container, text=name, font=("Segoe UI", 9), height=26,
                fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"],
                border_width=1, border_color=border_color, text_color=("#FFFFFF", "#E6EDF3"),
                command=lambda i=idx: self.run_command_by_index(i)
            )
            r = 1 + (btn_grid_idx // 2)
            c = btn_grid_idx % 2
            btn.grid(row=r, column=c, padx=2, pady=2, sticky="ew")
            btn_grid_idx += 1

    def add_command(self):
        self.edit_command(None)

    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select Command", "Select a command from the list first.")
            return
        self.edit_command(int(sel[0]))

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select Command", "Select a command to delete.")
            return
        idx = int(sel[0])
        cmd_name = self.commands_data[idx].get("name", "Command")
        if messagebox.askyesno("Confirm Delete", f"Delete '{cmd_name}'?"):
            self.commands_data.pop(idx)
            save_json(DATA_FILE, self.commands_data)
            self.refresh_command_tree()
            self.status_var.set(f"Deleted {cmd_name}")

    def edit_command(self, index):
        data = {"name": "", "category": "General", "type": "CMD Command", "target": "Selected DUT(s)", "command": ""}
        if index is not None:
            data = self.commands_data[index].copy()

        win = ctk.CTkToplevel(self)
        win.title("Edit Command" if index is not None else "Add Command")
        win.geometry("750x580")
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="Command Name:", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        name_entry = ctk.CTkEntry(win, font=("Segoe UI", 11))
        name_entry.insert(0, data["name"])
        name_entry.pack(fill="x", padx=16)

        ctk.CTkLabel(win, text="Category:", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(8, 2))
        cat_entry = ctk.CTkEntry(win, font=("Segoe UI", 11))
        cat_entry.insert(0, data["category"])
        cat_entry.pack(fill="x", padx=16)

        opts_frame = ctk.CTkFrame(win, fg_color="transparent")
        opts_frame.pack(fill="x", padx=16, pady=8)
        opts_frame.grid_columnconfigure(0, weight=1)
        opts_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(opts_frame, text="Command Type:", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        type_box = ctk.CTkComboBox(opts_frame, values=["CMD Command", "ADB Command", "Scrcpy Command", "Batch Script", "PowerShell Script", "Python Script"])
        type_box.set(data["type"])
        type_box.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=2)

        ctk.CTkLabel(opts_frame, text="Execution Target:", font=("Segoe UI", 11, "bold")).grid(row=0, column=1, sticky="w")
        target_box = ctk.CTkComboBox(opts_frame, values=["Selected DUT(s)", "Laptop"])
        target_box.set(data["target"])
        target_box.grid(row=1, column=1, sticky="ew", pady=2)

        ctk.CTkLabel(win, text="Command / Script: (use {SERIAL} for targeted DUT)", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(8, 2))
        cmd_text = ctk.CTkTextbox(win, font=("Consolas", 11), wrap="none")
        cmd_text.insert("1.0", data["command"])
        cmd_text.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        def save_cmd():
            name = name_entry.get().strip()
            cmd = cmd_text.get("1.0", "end-1c").strip()
            if not name or not cmd:
                messagebox.showerror("Error", "Command Name and Script cannot be empty.")
                return
            new_item = {
                "name": name, "category": cat_entry.get().strip() or "General",
                "type": type_box.get(), "target": target_box.get(), "command": cmd
            }
            if index is not None:
                self.commands_data[index] = new_item
            else:
                self.commands_data.append(new_item)
            save_json(DATA_FILE, self.commands_data)
            self.refresh_command_tree()
            win.destroy()

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(btn_bar, text="Save Command", fg_color=THEME["accent_blue"], hover_color=THEME["accent_blue_hover"], text_color=("#FFFFFF", "#E6EDF3"), command=save_cmd).pack(side="right")
        ctk.CTkButton(btn_bar, text="Cancel", fg_color=THEME["btn_neutral"], hover_color=THEME["btn_neutral_hover"], text_color=("#FFFFFF", "#E6EDF3"), command=win.destroy).pack(side="right", padx=6)

    def copy_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        cmd_str = self.commands_data[idx].get("command", "")
        self.clipboard_clear()
        self.clipboard_append(cmd_str)
        self.status_var.set("Command copied to clipboard.")

    def import_commands(self):
        path = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json"), ("Scripts", "*.bat *.cmd *.py *.txt")])
        if not path:
            return
        try:
            if path.endswith(".json"):
                data = load_json(path, [])
                if isinstance(data, list):
                    self.commands_data.extend(data)
            else:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                name = os.path.splitext(os.path.basename(path))[0]
                self.commands_data.append({"name": name, "category": "Imported", "type": "Batch Script", "target": "Selected DUT(s)", "command": content})
            save_json(DATA_FILE, self.commands_data)
            self.refresh_command_tree()
            self.status_var.set("Commands imported successfully.")
        except Exception as e:
            messagebox.showerror("Import Error", str(e))

    def export_commands(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Files", "*.json")])
        if path:
            save_json(path, self.commands_data)
            self.status_var.set("Commands exported.")

    def open_output_window(self):
        if self.output_window is not None:
            # Keep the detached output window in front when the user reopens it.
            self.output_window.deiconify()
            self.output_window.attributes("-topmost", True)
            self.output_window.lift()
            self.output_window.focus_force()
            self.output_window.after(100, lambda: self.output_window.attributes("-topmost", True) if self.output_window and self.output_window.winfo_exists() else None)
            return
        self.output_window = ctk.CTkToplevel(self)
        self.output_window.title("Command Output")
        self.output_window.geometry("850x500")
        # Detached output must stay above the main Command Center window.
        self.output_window.attributes("-topmost", True)
        self.output_window.lift()
        self.output_window.focus_force()
        txt = ctk.CTkTextbox(self.output_window, font=("Consolas", 10), text_color="#3FB950")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", self.console.get("1.0", "end"))

        def on_close():
            self.output_window.destroy()
            self.output_window = None
        self.output_window.protocol("WM_DELETE_WINDOW", on_close)

    def _get_device_operator(self, serial):
        """Read operator keys from one getprop snapshot instead of four ADB calls."""
        try:
            p = subprocess.run(
                ["adb", "-s", serial, "shell", "getprop"],
                capture_output=True, text=True, timeout=3, creationflags=WIN_NO_CONSOLE
            )
            props = {}
            for line in (p.stdout or "").splitlines():
                m = re.match(r"\[([^]]+)\]: \[(.*?)\]", line)
                if m:
                    props[m.group(1)] = m.group(2).strip()
            for key in ("gsm.operator.alpha", "gsm.sim.operator.alpha", "gsm.operator.alpha.2", "gsm.sim.operator.alpha.2"):
                value = props.get(key, "")
                if value and value.lower() not in ("unknown", "null", "n/a", "--"):
                    return value.replace("\r", "").replace("\n", " ").strip()
        except Exception:
            pass
        return "--"

    def _fetch_device_details(self, serial):
        """Fetch model/operator once per connection and update the card immediately when ready."""
        serial = str(serial or "").strip()
        if not serial or serial in self._device_details_inflight:
            return
        cached = self._device_details_cache.get(serial)
        if cached is not None:
            model, operator = cached
            self.after(0, self._apply_device_details, serial, model, operator)
            return
        self._device_details_inflight.add(serial)

        def worker():
            model = "Generic DUT"
            operator = "--"
            try:
                p = subprocess.run(
                    ["adb", "-s", serial, "shell", "getprop"],
                    capture_output=True, text=True, timeout=3, creationflags=WIN_NO_CONSOLE
                )
                props = {}
                for line in (p.stdout or "").splitlines():
                    m = re.match(r"\[([^]]+)\]: \[(.*?)\]", line)
                    if m:
                        props[m.group(1)] = m.group(2).strip()
                model = props.get("ro.product.model") or "Generic DUT"
                for key in ("gsm.operator.alpha", "gsm.sim.operator.alpha", "gsm.operator.alpha.2", "gsm.sim.operator.alpha.2"):
                    value = props.get(key, "")
                    if value and value.lower() not in ("unknown", "null", "n/a", "--"):
                        operator = value
                        break
            except Exception:
                pass

            def apply():
                self._device_details_inflight.discard(serial)
                if any(d.get("serial") == serial and d.get("status") == "device" for d in self.devices[:4]):
                    self._device_details_cache[serial] = (model, operator)
                    self._apply_device_details(serial, model, operator)
            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_device_details(self, serial, model, operator):
        for i, d in enumerate(self.devices[:4]):
            if d.get("serial") == serial and d.get("status") == "device":
                card = self.dut_cards[i]
                temp = card.fields["Temp"].cget("text") if "Temp" in card.fields else "--"
                card.update_status(serial, model, operator, temp, True)
                break

    def refresh_devices(self):
        def worker():
            try:
                p = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=8, creationflags=WIN_NO_CONSOLE)
                lines = [l.strip() for l in p.stdout.splitlines() if l.strip() and not l.lower().startswith("list of devices")]
                found = []
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] in ("device", "unauthorized", "offline"):
                        found.append({"serial": parts[0], "status": parts[1]})
                found = found[:4]
                self.devices = found

                def apply_base():
                    for i in range(4):
                        if i < len(found):
                            serial = found[i]["serial"]
                            conn = found[i]["status"] == "device"
                            card = self.dut_cards[i]
                            card.selected_var.set(conn)
                            card.update_status(serial, "--", "--", "--", conn)
                        else:
                            self.dut_cards[i].selected_var.set(False)
                            self.dut_cards[i].update_status("", "", "", "--", False)
                    self.status_var.set(f"{sum(d.get('status') == 'device' for d in found)} device(s) detected.")
                    self._update_qf_target_display()
                    for d in found:
                        if d["status"] == "device":
                            serial = d["serial"]
                            if serial not in self._device_details_cache:
                                self._fetch_device_details(serial)
                self.after(0, apply_base)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _fast_connection_watch(self):
        """250 ms ADB polling; state and details update through a fast UI path."""
        def worker():
            try:
                p = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=2, creationflags=WIN_NO_CONSOLE)
                lines = [l.strip() for l in p.stdout.splitlines() if l.strip() and not l.lower().startswith("list of devices")]
                current = []
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] in ("device", "unauthorized", "offline"):
                        current.append((parts[0], parts[1]))
                current = current[:4]
                old_map = {d.get("serial"): d.get("status") for d in self.devices}
                new_map = {serial: state for serial, state in current}
                if old_map != new_map:
                    self.connection_state = [state == "device" for _, state in current] + [False] * (4 - len(current))
                    self.devices = [{"serial": serial, "status": state} for serial, state in current]

                    def apply_instant_status():
                        # Keep cards mapped to serials; do not let list ordering
                        # shift a disconnect onto another DUT card.
                        for i in range(4):
                            self.dut_cards[i].selected_var.set(False)
                            self.dut_cards[i].update_status("", "", "", "--", False)
                        for i, (serial, adb_state) in enumerate(current):
                            connected = adb_state == "device"
                            card = self.dut_cards[i]
                            card.selected_var.set(connected)
                            card.update_status(serial, "--", "--", "--", connected)
                        self.status_var.set(f"{sum(s == 'device' for _, s in current)} device(s) detected.")
                        self._update_qf_target_display()
                        for serial, adb_state in current:
                            if adb_state == "device" and serial not in self._device_details_cache:
                                self._fetch_device_details(serial)
                    self.after(0, apply_instant_status)
            except Exception:
                pass
            finally:
                self.after(250, self._fast_connection_watch)
        threading.Thread(target=worker, daemon=True).start()

    def _live_temperature_watch(self):
        def worker():
            for i, d in enumerate(self.devices[:4]):
                if d.get("status") == "device":
                    ser = d.get("serial")
                    try:
                        out = subprocess.run(["adb", "-s", ser, "shell", "dumpsys", "battery"], capture_output=True, text=True, timeout=4, creationflags=WIN_NO_CONSOLE).stdout
                        m = re.search(r"(?im)^\s*temperature:\s*(-?\d+)\s*$", out or "")
                        operator = self._get_device_operator(ser)
                        if m:
                            val = int(m.group(1)) / 10.0
                            self.after(0, lambda idx=i, v=val, op=operator: self.dut_cards[idx].update_status(
                                self.devices[idx].get("serial"), self.dut_cards[idx].fields["Model"].cget("text"),
                                op if op != "--" else self.dut_cards[idx].fields["Operator"].cget("text"), f"{v:.1f} °C", True
                            ))
                        elif operator != "--":
                            self.after(0, lambda idx=i, op=operator: self.dut_cards[idx].fields["Operator"].configure(text=op))
                    except Exception:
                        pass
            self.after(3000, self._live_temperature_watch)
        threading.Thread(target=worker, daemon=True).start()

    def run_command_by_index(self, index):
        if index < 0 or index >= len(self.commands_data):
            return
        cmd_obj = self.commands_data[index]
        self.execute_command_object(cmd_obj)

    def run_selected_commands(self):
        items = self.tree.selection()
        if not items:
            self.log("[WARN] No commands selected in library.")
            return
        for item in items:
            self.run_command_by_index(int(item))

    def execute_command_object(self, cmd_obj):
        command = cmd_obj.get("command", "")
        name = cmd_obj.get("name", "Command")
        typ = cmd_obj.get("type", "CMD Command")
        target = cmd_obj.get("target", "Selected DUT(s)")

        self.log(f"\n===== EXECUTE: {name} =====")

        # Laptop-targeted commands are intentionally not rewritten with an ADB
        # serial. DUT-targeted commands use ONLY the DUT checkboxes selected by
        # the operator. This rule is shared by Command Library, Quick Features,
        # and newly saved commands because all three call this same executor.
        if target == "Laptop":
            self._run_process(command, label="LAPTOP")
            return

        targets = self.selected_devices()
        if not targets:
            self._update_qf_target_display()
            messagebox.showwarning("No DUT Selected", "Select at least one connected DUT checkbox before running this command.")
            return

        self.log(f"> Target DUTs: {len(targets)} | " + ", ".join(f"DUT #{i}={serial}" for i, serial in targets))

        if typ == "Scrcpy Command":
            for idx, (_, serial) in enumerate(targets):
                self.launch_scrcpy_serial(serial, index=idx, count=len(targets))
            return

        for _, serial in targets:
            # Every ADB invocation in the command receives this exact DUT's
            # serial. {SERIAL} is also expanded for scripts/commands that need
            # the serial as an argument or environment-independent placeholder.
            dut_cmd = self._target_adb_commands(command, serial)
            if typ in ("Batch Script", "PowerShell Script"):
                self._run_temp_script(dut_cmd, typ, serial)
            else:
                self._run_process(dut_cmd, label=serial)


    def _run_process(self, cmd_str, label=""):
        def worker():
            try:
                p = subprocess.Popen(cmd_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=WIN_NO_CONSOLE)
                with self.active_processes_lock:
                    self.active_processes.add(p)
                for line in iter(p.stdout.readline, ""):
                    if self.stop_event.is_set():
                        p.terminate()
                        break
                    self.log(f"[{label}] {line.strip()}")
                p.stdout.close()
                p.wait()
                with self.active_processes_lock:
                    self.active_processes.discard(p)
            except Exception as e:
                self.log(f"[{label}] Error: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _run_temp_script(self, script_text, typ, label):
        suffix = ".ps1" if typ == "PowerShell Script" else ".bat"
        fd, path = tempfile.mkstemp(prefix=f"cc_{label}_", suffix=suffix)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(script_text)
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", path] if typ == "PowerShell Script" else ["cmd", "/c", path]
        self._run_process(subprocess.list2cmdline(cmd), label=label)

    def stop_execution(self):
        self.stop_event.set()
        with self.active_processes_lock:
            for p in list(self.active_processes):
                try:
                    p.terminate()
                except Exception:
                    pass
        self.log("[SYSTEM] Stop execution signal sent.")
        self.after(500, self.stop_event.clear)

    def start_scrcpy_button(self):
        """Launch independent scrcpy processes for every connected DUT.

        Single- and multi-device launches use the same path.  Each serial gets
        its own process entry, and all launches are started concurrently so one
        slow/failed device cannot hold the other windows behind it.
        """
        chosen = self._connected_devices_all()
        if not chosen:
            messagebox.showwarning("No Connected DUTs", "No connected DUTs were detected.")
            return

        exe = normalize_scrcpy(self.settings.get("scrcpy_path", ""))
        if not exe:
            exe = filedialog.askopenfilename(
                title="Select scrcpy.exe",
                filetypes=[("scrcpy executable", "scrcpy.exe"), ("Executable files", "*.exe")]
            )
            exe = normalize_scrcpy(exe)
            if not exe:
                messagebox.showwarning("scrcpy Not Configured", "Please select scrcpy.exe to continue.")
                return
            self.settings["scrcpy_path"] = exe
            save_json(SETTINGS_FILE, self.settings)

        self.log(f"\n===== SCRCPY ({len(chosen)} DUT(s)) =====")
        # Do not serialize launches.  Starting every process independently is
        # what makes 2/3/4 connected DUTs open in one click.
        for idx, (_, serial) in enumerate(chosen):
            self.launch_scrcpy_serial(serial, index=idx, count=len(chosen), exe=exe)

    def launch_scrcpy_serial(self, serial, index=0, count=1, exe=None):
        if not exe:
            exe = detect_scrcpy_fast(self.settings.get("scrcpy_path", ""))
        if not exe:
            self.log(f"[{serial}] Error: scrcpy.exe could not be found.")
            return

        # Reserve the serial before starting the worker. This prevents repeated
        # clicks from racing and launching duplicate windows for the same DUT.
        with self.scrcpy_lock:
            existing = self.scrcpy_processes.get(serial)
            if existing is not None and existing.poll() is None:
                self.log(f"[{serial}] scrcpy is already running; keeping the existing window.")
                return
            if serial in self.scrcpy_launching:
                self.log(f"[{serial}] scrcpy launch already in progress; keeping the existing attempt.")
                return
            self.scrcpy_launching.add(serial)
            self.scrcpy_processes.pop(serial, None)

        def worker():
            try:
                # The connected-device list was already obtained by the caller.
                # A second adb get-state here used to serialize/slow multi-launch
                # and could race during reconnects. Let scrcpy perform its own
                # serial-specific transport check instead.
                cell_w = 460
                margin = 20
                x = margin + (index * cell_w)
                y = 40

                cmd = [
                    exe, "-s", serial,
                    "--window-title", f"{serial} - scrcpy",
                    "--window-x", str(x),
                    "--window-y", str(y),
                    "--max-size", "800"
                ]

                self.log(f"[{serial}] Starting scrcpy...")
                p = subprocess.Popen(
                    cmd,
                    cwd=os.path.dirname(exe),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    creationflags=WIN_NO_CONSOLE
                )

                with self.scrcpy_lock:
                    self.scrcpy_processes[serial] = p
                    self.scrcpy_launching.discard(serial)

                threading.Thread(
                    target=self._monitor_scrcpy_process,
                    args=(serial, p),
                    daemon=True
                ).start()

                # Do not wait for window creation here.  Popen returning means the
                # GUI process has been handed off; other DUTs must launch without
                # waiting for this one's video/window initialization.
                self.log(f"[{serial}] scrcpy process launched.")

            except Exception as e:
                with self.scrcpy_lock:
                    self.scrcpy_launching.discard(serial)
                    self.scrcpy_processes.pop(serial, None)
                self.log(f"[{serial}] Failed to launch scrcpy: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _monitor_scrcpy_process(self, serial, process):
        """Collect scrcpy diagnostics and clear the process registry when it exits."""
        try:
            out, err = process.communicate()
            if out and out.strip():
                for line in out.splitlines():
                    self.log(f"[{serial}] scrcpy: {line.strip()}")
            if err and err.strip():
                for line in err.splitlines():
                    if line.strip():
                        self.log(f"[{serial}] scrcpy: {line.strip()}")
            rc = process.returncode
            if rc not in (0, None):
                self.log(f"[{serial}] scrcpy closed with exit code {rc}.")
        except Exception as e:
            self.log(f"[{serial}] scrcpy monitor error: {e}")
        finally:
            with self.scrcpy_lock:
                if self.scrcpy_processes.get(serial) is process:
                    self.scrcpy_processes.pop(serial, None)

    def open_go_batch(self):
        path = self.settings.get("go_batch_path", "")
        if path and os.path.exists(path):
            folder = os.path.dirname(path) if os.path.isfile(path) else path
            subprocess.Popen(["cmd.exe", "/k", "python go.py"], cwd=folder, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.log("[ACTION] Go Batch triggered in separate terminal.")
        else:
            sel = filedialog.askopenfilename(title="Select go.py", filetypes=[("Python Files", "go.py")])
            if sel:
                self.settings["go_batch_path"] = sel
                save_json(SETTINGS_FILE, self.settings)
                self.open_go_batch()

    def browse_generic_tool(self, key, var, name):
        if key == "scrcpy_path":
            folder = filedialog.askdirectory(title="Select scrcpy directory")
            if folder:
                exe = normalize_scrcpy(folder)
                if exe:
                    self.settings[key] = exe
                    save_json(SETTINGS_FILE, self.settings)
                    var.set(exe)
                    messagebox.showinfo("Success", f"scrcpy configured: {exe}")
                else:
                    messagebox.showerror("Error", "scrcpy.exe was not found in selected directory.")
        else:
            f = filedialog.askopenfilename(title=f"Select {name}")
            if f:
                self.settings[key] = f
                save_json(SETTINGS_FILE, self.settings)
                var.set(f)

    def open_generic_tool(self, key, name):
        p = self.settings.get(key, "")
        if key == "scrcpy_path":
            self.start_scrcpy_button()
            return

        if p and os.path.exists(p):
            if p.endswith(".jar"):
                subprocess.Popen(["java", "-jar", os.path.basename(p)], cwd=os.path.dirname(p))
            else:
                subprocess.Popen([p], cwd=os.path.dirname(p))
        else:
            messagebox.showwarning("Not Configured", f"Please browse or search to configure {name} first.")


if __name__ == "__main__":
    app = CommandCenterApp()
    app.mainloop()

#!/usr/bin/env python3
"""
PyTGS Graphical Interface with configuration editing and multi-scope support
"""

import sys
import os
import threading
import queue
import logging
import yaml
import shutil
import json
import numpy as np
import struct
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib
# Set backend before importing pyplot
try:
    # Check if we're in a GUI environment
    if sys.platform == 'win32':
        # On Windows, try to use TkAgg but with warnings suppressed
        pass  # Keep default TkAgg for GUI
    else:
        matplotlib.use('Agg')
except:
    pass
import matplotlib.pyplot as plt
matplotlib.rcParams['figure.max_open_warning'] = 0
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
warnings.filterwarnings("ignore", category=UserWarning, module="pyvisa_py") 
import ttkbootstrap as tb
import time
#import gpib_ctypes
# Check if we're using GPIB address
scope_addr = os.environ.get('SCOPE_ADDRESS', '')
if 'GPIB' not in scope_addr:
    # Suppress the GPIB warning since we're not using GPIB
    warnings.filterwarnings("ignore", category=UserWarning, module="gpib_ctypes")

# Import PyTGS modules
from src.analysis.signal_process import process_signal
from src.analysis.fft import fft
from src.analysis.lorentzian import lorentzian_fit
from src.core.path import Paths

# Suppress pyvisa_py logging at the source
os.environ['PYVISA_LOGGING'] = 'CRITICAL'
os.environ['PYVISA_PY_LOGGING'] = 'CRITICAL'

# Set up logging to suppress pyvisa_py completely
logging.getLogger('pyvisa').setLevel(logging.CRITICAL)
logging.getLogger('pyvisa_py').setLevel(logging.CRITICAL)

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    
    return os.path.join(base_path, relative_path)

# Redirect logging to a queue for GUI display
log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue
    def emit(self, record):
        # Only show INFO and above in GUI (no DEBUG)
        if record.levelno >= logging.INFO:
            self.queue.put(self.format(record))

class ToolTip:
    """Create tooltips for widgets with automatic positioning to stay on screen"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind('<Enter>', self.enter)
        widget.bind('<Leave>', self.leave)
    
    def enter(self, event=None):
        root = self.widget.winfo_toplevel()
        
        # Get cursor position using the root window's method
        # This should give coordinates relative to the root window's screen
        cursor_x = root.winfo_pointerx()
        cursor_y = root.winfo_pointery()
        
        self.tip_window = tw = tk.Toplevel(root)
        tw.wm_overrideredirect(True)
        
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                        background="#ffffe0", foreground="#000000",
                        relief=tk.SOLID, borderwidth=1,
                        font=("Arial", 9), wraplength=400)
        label.pack()
        
        tw.update_idletasks()
        tip_width = tw.winfo_width()
        tip_height = tw.winfo_height()
        
        offset_x = 15
        offset_y = 20
        
        pos_x = cursor_x + offset_x
        pos_y = cursor_y + offset_y
        
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        
        if pos_x + tip_width > screen_width:
            pos_x = cursor_x - tip_width - offset_x
        if pos_x < 0:
            pos_x = 5
        if pos_y + tip_height > screen_height:
            pos_y = cursor_y - tip_height - offset_y
        if pos_y < 0:
            pos_y = 5
        
        tw.wm_geometry(f"+{int(pos_x)}+{int(pos_y)}")
    
    def leave(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

class TGSApp:
    VERSION = "1.0.1"

    # Scope type constants
    SCOPE_RIGOL = "Rigol"
    SCOPE_LECROY = "LeCroy"

    def __init__(self, root):
        self.root = root
        self.root.title("PyTGS v" + self.VERSION + " - Transient Grating Analyser")
        self.root.geometry("1700x1000")
        
        # Set theme to match system
        self.setup_theme()
        
        # Initialize basic attributes BEFORE loading config that might log
        self.config = None
        self.calibrated_spacing = None
        
        # File storage - initialize early
        self.pos_files = []
        self.neg_files = []
        self.calib_pos_file = ''
        self.calib_neg_file = ''
        self.baseline_pos_file = ''
        self.baseline_neg_file = ''
        self.file_to_fit_plot_data = {}
        self.file_to_plot_path = {}
        self.file_to_fit_params = {}
        self.current_results_log_path = None
        self.shutdown_flag = False
        self.stop_batch = False

        # Setup logging 
        self.setup_logging()

        #Setup batch process file tracking
        self.processed_files = set()
        
        # Load fitting configuration
        self.config = self.load_config()
        
        # Scope settings initialisation
        self.orig_scale = None
        self.orig_offset = None
        self.scope_connection_string = None
        
        # Current scope type (default Rigol)
        self.current_scope_type = self.SCOPE_RIGOL

        # Build GUI
        self.create_widgets()
        self.setup_preference_saving()
        self.root.bind('<Configure>', self.on_window_resize)
        self.resize_timer = None
        self.root.after(500, self.refresh_scope_addresses)
        self.load_preferences()
        self.continuous_acq_var.set(False)
        
        # Bind closing event
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.running_job = None

        # Show placeholder text in results table (now after treeview is created)
        self.clear_results_table()
        
        # Time tracking for batch processing
        self.batch_start_time = None
        self.first_run_duration = None
        self.current_run_start_time = None

        # Track scope connection state
        self.scope_connected = False
        
        # Test connection to oscilloscope at startup
        self.test_scope_connection()

        print(f"[INIT] PyTGS v{self.VERSION} starting...")

        self.memory_monitor_active = True
        self.start_memory_monitor()
    
    def start_memory_monitor(self):
            """Start a background thread to monitor memory usage"""
            def monitor_thread():
                import time
                while self.memory_monitor_active:
                    try:
                        mem = self.get_memory_usage()
                        if mem and mem['rss'] > 2000:  # Warning if > 2GB
                            print(f"[WARNING] Memory usage high: {mem['rss']:.1f}MB")
                            self.log_message(f"Memory usage high: {mem['rss']:.1f}MB", logging.WARNING)
                            # Force garbage collection
                            gc.collect()
                    except:
                        pass
                    time.sleep(30)  # Check every 30 seconds
            
            thread = threading.Thread(target=monitor_thread, daemon=True)
            thread.start()

    def create_button(self, parent, text, command=None, state='normal', height=None, bg_color=None, fg_color=None):
        """Create a styled button with consistent hover effects"""
        # Default padding - reduced height by setting smaller pady
        padx_val = 12
        pady_val = 3 if height == 'small' else 6
        
        # Use custom colors if provided, otherwise use theme defaults
        button_bg = bg_color if bg_color else self.button_color
        button_fg = fg_color if fg_color else self.fg_color
        
        # For red buttons, use a darker hover color
        if bg_color == '#d32f2f':
            hover_bg = '#b71c1c'  # Darker red for hover
        else:
            hover_bg = self.accent_hover
        
        btn = tk.Button(parent, text=text,
                    bg=button_bg, fg=button_fg,
                    activebackground=hover_bg,
                    activeforeground=button_fg,
                    relief=tk.FLAT, bd=0,
                    padx=padx_val, pady=pady_val,
                    font=("Arial", 9 if height == 'small' else 10, 'bold' if bg_color else 'normal'),
                    cursor="hand2",
                    state=state)
        
        # Only set the command once
        if command and state == 'normal':
            btn.config(command=command)
        
        # Add hover effects
        def on_enter(e):
            if state == 'normal':
                btn.config(bg=hover_bg)
        
        def on_leave(e):
            if state == 'normal':
                btn.config(bg=button_bg)
        
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        
        # Configure the button to expand and fill the available space
        btn.config(highlightthickness=0)
        
        return btn

    def setup_theme(self):
        """Configure modern dark theme - remove all borders"""
        # Colors
        self.bg_color = '#1e1e1e'
        self.panel_bg = '#2d2d2d'
        self.fg_color = '#e0e0e0'
        self.select_color = '#404040'
        self.button_color = '#3c3c3c'
        self.entry_bg = '#3c3c3c'
        self.accent_hover = '#005a9e'
        
        self.root.configure(background=self.bg_color)
        
        style = ttk.Style()
        
        # Remove borders from ttk widgets
        style.configure("TFrame", borderwidth=0, relief="flat")
        style.configure("TLabelframe", borderwidth=0, relief="flat")
        style.configure("TLabelframe.Label", borderwidth=0, relief="flat")
        style.configure("TPanedwindow", borderwidth=0, relief="flat")
        style.configure("TPanedwindow.Sash", borderwidth=0)
        
        # Treeview
        style.configure("Treeview", 
                        background=self.entry_bg, 
                        foreground=self.fg_color, 
                        fieldbackground=self.entry_bg)
        style.configure("Treeview.Heading", 
                        background=self.button_color, 
                        foreground=self.fg_color)
        style.map('Treeview', 
                background=[('selected', self.select_color)])
        
        style.configure('Panel.TFrame', background=self.panel_bg)

    def on_window_resize(self, event):
        """Maintain sash position proportion when window is resized (debounced)"""
        # Debounce: wait for resize to finish before updating
        if self.resize_timer:
            self.root.after_cancel(self.resize_timer)
        
        # Schedule the actual resize handling after a short delay
        self.resize_timer = self.root.after(100, lambda: self._handle_resize(event))

    def _handle_resize(self, event):
        """Actually handle the resize after debouncing"""
        # Only update sash positions
        if hasattr(self, 'paned_window') and event.widget == self.root:
            total_width = self.root.winfo_width()
            if total_width > 100:
                # Maintain proportions: Left 35%, Middle 25%, Right 40%
                try:
                    self.paned_window.sashpos(0, int(total_width * 0.30))
                    self.paned_window.sashpos(1, int(total_width * 0.60))
                except:
                    pass
        
        if hasattr(self, 'right_paned') and event.widget == self.root:
            total_height = self.root.winfo_height()
            if total_height > 100:
                new_sash_pos = int(total_height * 0.4)
                try:
                    self.right_paned.sashpos(0, new_sash_pos)
                except:
                    pass
        
        if hasattr(self, 'right_paned') and event.widget == self.root:
            total_height = self.root.winfo_height()
            if total_height > 100:
                new_sash_pos = int(total_height * 0.4)
                try:
                    self.right_paned.sashpos(0, new_sash_pos)
                except:
                    pass

    def set_initial_sash(self):
        """Set initial divider position for 3 panels"""
        try:
            total_width = self.root.winfo_width()
            if total_width > 100:
                self.paned_window.sashpos(0, int(total_width * 0.30))  # Left panel wider
                self.paned_window.sashpos(1, int(total_width * 0.60))  # Middle + Left = 60% (so middle is 25%)
            
            if hasattr(self, 'right_paned') and self.right_paned.winfo_exists():
                total_height = self.root.winfo_height()
                if total_height > 100:
                    self.right_paned.sashpos(0, int(total_height * 0.4))
        except:
            pass

    def set_ui_state(self, state):
        """Simple UI state management - only disable run button during batch"""
        self.current_ui_state = state
        
        # Only disable the run button during batch processing to prevent re-run
        if state == 'batch_processing':
            if hasattr(self, 'run_button'):
                self.run_button.config(state='disabled')
        else:
            if hasattr(self, 'run_button'):
                self.run_button.config(state='normal')

    def load_config(self):
        """Load config.yaml from the current directory or create default."""
        # Add custom YAML constructor for numpy scalars
        def construct_numpy_scalar(self, node):
            import numpy as np
            value = self.construct_scalar(node)
            try:
                # Try to convert to float first
                return float(value)
            except ValueError:
                return value
        
        # Add constructor for numpy array tag
        def construct_numpy_array(self, node):
            import numpy as np
            value = self.construct_sequence(node)
            # Convert numpy scalars to Python types
            return [float(x) if hasattr(x, 'dtype') else x for x in value]
        
        # Add the constructors to yaml
        try:
            from yaml import SafeLoader, add_constructor
            add_constructor('tag:yaml.org,2002:python/object/apply:numpy._core.multiarray.scalar', construct_numpy_scalar)
            add_constructor('tag:yaml.org,2002:python/object/apply:numpy.core.multiarray.scalar', construct_numpy_scalar)
            add_constructor('tag:yaml.org,2002:python/object/apply:numpy.ndarray', construct_numpy_array)
        except:
            pass
        
        # Check if running as executable
        if getattr(sys, 'frozen', False):
            # Running as compiled executable - look for config in the same directory as the exe
            config_path = Path(os.path.dirname(sys.executable)) / "config.yaml"
        else:
            # Running as script
            config_path = Path("config.yaml")
        
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    config_data = yaml.safe_load(f)
                
                # Clean any numpy types from the config recursively
                config_data = self._clean_numpy_types(config_data)
                return config_data
            except Exception as e:
                self.log_message(f"Error loading config, creating new one: {str(e)}", logging.WARNING)
                # If config is corrupted, create backup and new default
                if config_path.exists():
                    backup_path = config_path.with_suffix('.yaml.bak')
                    try:
                        shutil.copy(config_path, backup_path)
                        self.log_message(f"Backed up corrupted config to {backup_path}", logging.WARNING)
                    except:
                        pass
                return self._create_default_config()
        else:
            return self._create_default_config()

    def _clean_numpy_types(self, obj):
        """Recursively convert numpy types to Python native types"""
        import numpy as np
        
        if isinstance(obj, dict):
            return {key: self._clean_numpy_types(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._clean_numpy_types(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(self._clean_numpy_types(item) for item in obj)
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, np.bool_):
            return bool(obj)
        else:
            return obj

    def _create_default_config(self):
        """Create default configuration dictionary"""
        return {
            "path": "example",
            "study_names": None,
            "idxs": None,
            "signal_process": {
                "heterodyne": "di-homodyne",
                "null_point": 2,
                "initial_samples": 50,
                "baseline_correction": {"enabled": False, "pos": None, "neg": None}
            },
            "fft": {"signal_proportion": 1.0, "use_derivative": True, "analysis_type": "psd"},
            "lorentzian": {
                "signal_proportion": 1,
                "frequency_bounds": [0.1, 0.9],
                "dc_filter_range": [0, 50000],
                "bimodal_fit": False,
                "use_skewed_super_lorentzian": False
            },
            "tgs": {"grating_spacing": 3.5276, "signal_proportion": 1, "maxfev": 1000000},
            "plot": {
                "signal_process": True,
                "fft_lorentzian": True,
                "tgs": True,
                "settings": {"num_points": None}
            }
        }
    
    def save_config(self):
        """Save current config to config.yaml."""
        # Clean numpy types before saving
        cleaned_config = self._clean_numpy_types(self.config)
        
        # Save to the appropriate location
        if getattr(sys, 'frozen', False):
            config_path = Path(os.path.dirname(sys.executable)) / "config.yaml"
        else:
            config_path = Path("config.yaml")
        
        with open(config_path, 'w') as f:
            yaml.dump(cleaned_config, f, sort_keys=False, default_flow_style=False)
        self.log_message("Configuration saved to config.yaml")

    def factory_reset_config(self, editor_window=None):
        """Reset configuration to factory defaults"""
        # Confirm with user
        if not messagebox.askyesno("Factory Reset", 
                                "This will reset all settings to factory defaults.\n\n"
                                "Any unsaved changes will be lost.\n\n"
                                "Do you want to continue?"):
            return
        
        # Factory default configuration
        default_config = {
            "path": "example",
            "study_names": None,
            "idxs": None,
            "signal_process": {
                "heterodyne": "di-homodyne",
                "null_point": 2,
                "initial_samples": 50,
                "baseline_correction": {"enabled": False, "pos": None, "neg": None}
            },
            "fft": {"signal_proportion": 1.0, "use_derivative": True, "analysis_type": "psd"},
            "lorentzian": {
                "signal_proportion": 1,
                "frequency_bounds": [0.1, 0.9],
                "dc_filter_range": [0, 50000],
                "bimodal_fit": False,
                "use_skewed_super_lorentzian": False
            },
            "tgs": {"grating_spacing": 3.5276, "signal_proportion": 1, "maxfev": 1000000},
            "plot": {
                "signal_process": True,
                "fft_lorentzian": True,
                "tgs": True,
                "settings": {"num_points": None}
            }
        }
        
         # Update config
        self.config = default_config
        
        # Update main GUI parameters
        self.start_point_var.set(2)
        self.two_saw_var.set(False)
        self.baseline_var.set(False)
        self.grating_edit.delete(0, tk.END)
        self.grating_edit.insert(0, "3.5276")
        
        # Save to default config.yaml
        self.save_config()
        
        self.log_message("Configuration reset to factory defaults")
        
        if editor_window:
            # Close and reopen editor to show defaults
            editor_window.destroy()
            self.open_config_editor()
        else:
            pass

    def load_config_file(self, editor_window=None):
        """Load configuration from a user-selected YAML file"""
        file_path = filedialog.askopenfilename(
            filetypes=[
                ("YAML files", "*.yaml *.yml"),
                ("All files", "*.*")
            ],
            title="Load Configuration File"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'r') as f:
                loaded_config = yaml.safe_load(f)
            
            # Update current config with loaded values
            self.config.update(loaded_config)
            
            # Update main GUI parameters from loaded config
            self.start_point_var.set(self.config['signal_process']['null_point'])
            self.two_saw_var.set(self.config['lorentzian']['bimodal_fit'])
            self.baseline_var.set(self.config['signal_process']['baseline_correction']['enabled'])
            
            # Update grating spacing
            if 'tgs' in self.config and 'grating_spacing' in self.config['tgs']:
                self.grating_edit.delete(0, tk.END)
                self.grating_edit.insert(0, f"{self.config['tgs']['grating_spacing']:.6f}")
            
            # Save to default config.yaml
            self.save_config()
            
            self.log_message(f"Configuration loaded from: {file_path}")
            
            if editor_window:
                # Close the existing editor and reopen with new config
                editor_window.destroy()
                self.open_config_editor()
            else:
                pass
                
        except Exception as e:
            self.log_message(f"Error loading configuration: {str(e)}", logging.ERROR)
            if editor_window:
                messagebox.showerror("Error", f"Failed to load configuration:\n{str(e)}")

    def test_scope_connection(self):
        """Test connection to the oscilloscope at startup and log the result"""
        # Don't test if we're shutting down
        if hasattr(self, 'shutdown_flag') and self.shutdown_flag:
            return
            
        scope_address = self.scope_address_var.get().strip()
        
        if not scope_address:
            self.log_message("No oscilloscope address configured", logging.WARNING)
            return
        
        self.log_message(f"Testing connection to {self.current_scope_type} at {scope_address}...")
        
        # Disable UI during test and show testing status
        self.acq_status_var.set("Testing connection...")
        
        def ping_thread():
            # Check shutdown flag before proceeding
            if hasattr(self, 'shutdown_flag') and self.shutdown_flag:
                return
            
            try:
                import pyvisa
                import time
                
                # PyVISA automatically handles the underlying DLL loading (e.g., visa64.dll)
                # Ensure NI-VISA Runtime is installed on the system.
                rm = pyvisa.ResourceManager()
                
                # Try to connect with a short timeout
                scope = rm.open_resource(scope_address)
                scope.timeout = 1000
                scope.read_termination = '\n'
                scope.write_termination = '\n'
                
                # Try a simple command
                try:
                    scope.write("*CLS")
                except Exception as e:
                    # Cleanup on failure
                    try: scope.close()
                    except: pass
                    try: rm.close()
                    except: pass
                    if not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                        self.root.after(0, lambda: self.log_message(f"  Basic communication failed: {str(e)}"))
                    return
                
                # Get instrument identification
                idn = None
                if not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                    try:
                        idn = scope.query("*IDN?").strip()
                    except:
                        pass
                
                # Cleanup
                try: scope.close()
                except: pass
                try: rm.close()
                except: pass
                
                if idn and not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                    idn_parts = idn.split(',')
                    model = idn_parts[1] if len(idn_parts) > 1 else "Unknown"
                    
                    def handle_success():
                        if not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                            self.log_message(f"Scope connected: {model}")
                            self.acq_status_var.set("Ready - Scope connected")
                    self.root.after(0, handle_success)
                elif not idn and not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                    def handle_error():
                        if not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                            self.log_message("Scope connection failed: No response", logging.ERROR)
                            self.acq_status_var.set("Not ready - Scope NOT connected")
                    self.root.after(0, handle_error)
                    
            except Exception as e:
                error_msg = str(e)
                # Cleanup and log error
                if not (hasattr(self, 'shutdown_flag') and self.shutdown_flag):
                    def handle_error():
                        self.log_message(f"Scope connection failed: {error_msg[:100]}", logging.ERROR)
                        self.acq_status_var.set("Not ready - Scope NOT connected")
                    self.root.after(0, handle_error)
        
        # Start thread
        thread = threading.Thread(target=ping_thread)
        thread.daemon = True
        thread.start()
    
    def scan_available_resources(self):
        """Scan for available VISA resources and return a list of addresses"""
        resources = []
        try:
            import pyvisa
            
            # Try multiple backends
            backends = ['@py', '@ivi', '']
            for backend in backends:
                try:
                    if backend:
                        rm = pyvisa.ResourceManager(backend)
                    else:
                        rm = pyvisa.ResourceManager()
                    
                    # Get list of resources
                    available = rm.list_resources()
                    rm.close()
                    
                    if available:
                        resources = list(available)
                        break
                except Exception:
                    continue
                    
        except ImportError:
            self.log_message("PyVISA not available for resource scanning", logging.WARNING)
        except Exception as e:
            self.log_message(f"Error scanning resources: {str(e)}", logging.WARNING)
        
        return resources

    def setup_logging(self):
        """Redirect logging to a queue for display in GUI."""
        self.logger = logging.getLogger()
        self.logger.setLevel(logging.INFO)
        
        # Remove existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # Queue handler for GUI (INFO and above only)
        self.queue_handler = QueueHandler(log_queue)
        self.queue_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        self.logger.addHandler(self.queue_handler)
        
        # Suppress matplotlib debug messages
        logging.getLogger('matplotlib').setLevel(logging.WARNING)
        logging.getLogger('PIL').setLevel(logging.WARNING)
        
        # Suppress pyvisa_py verbose logging - THIS IS KEY
        logging.getLogger('pyvisa_py').setLevel(logging.CRITICAL)  # Only show CRITICAL errors
        logging.getLogger('pyvisa_py.protocols').setLevel(logging.ERROR)
        logging.getLogger('pyvisa_py.tcpip').setLevel(logging.ERROR)
        logging.getLogger('pyvisa').setLevel(logging.ERROR)
        
        self.poll_log_queue()
    
    def poll_log_queue(self):
        """Periodically update the log text widget."""
        try:
            while True:
                msg = log_queue.get_nowait()
                self.log_text.insert(tk.END, msg + '\n')
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.poll_log_queue)
    
    def log_message(self, msg, level=logging.INFO):
        """Helper to log a message."""
        self.logger.log(level, msg)
    
    def create_widgets(self):
        """Create main layout - adjustable divider"""
        # Main frame
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill='both', expand=True, padx=10, pady=10)
        
        # Create a PanedWindow for left/right split
        self.main_paned = ttk.PanedWindow(main_frame, orient='horizontal')
        self.main_paned.pack(fill='both', expand=True)
        
        # LEFT SIDE: Vertical paned window for acquisition + results summary
        left_panel = ttk.Frame(self.main_paned)
        self.left_paned = ttk.PanedWindow(left_panel, orient='vertical')
        self.left_paned.pack(fill='both', expand=True)

        # Acquisition panel (top-left) - let it use its natural height
        acq_container = ttk.Frame(self.left_paned)
        acq_inner = ttk.Frame(acq_container)
        acq_inner.pack(fill='both', expand=True, padx=10, pady=5)

        # Build acquisition section (ONCE)
        self.build_acquisition_section(acq_inner)

        # Force layout update and get the natural height
        self.root.update_idletasks()
        acq_natural_height = acq_container.winfo_reqheight()

        # Add the acquisition container with weight 0 (won't expand)
        self.left_paned.add(acq_container, weight=0)

        # Results Summary panel (bottom-left) - takes all remaining space
        results_summary_container = ttk.Frame(self.left_paned)
        results_summary_inner = ttk.Frame(results_summary_container)
        results_summary_inner.pack(fill='both', expand=True, padx=10, pady=5)

        # Build results summary section (ONCE)
        self.build_results_summary_section(results_summary_inner)

        # Add the results container with weight 1 (expands to fill space)
        self.left_paned.add(results_summary_container, weight=1)

        self.main_paned.add(left_panel, weight=2)  # Left side takes 20% width
        
        # Middle panel for controls (existing)
        middle_panel = ttk.Frame(self.main_paned)
        middle_inner = ttk.Frame(middle_panel)
        middle_inner.pack(fill='both', expand=True, padx=(10, 10), pady=5)
        self.main_paned.add(middle_panel, weight=3)  # 30% width
        
        # Right panel for log output
        right_panel = ttk.Frame(self.main_paned)
        right_inner = ttk.Frame(right_panel)
        right_inner.pack(fill='both', expand=True, padx=(10, 0), pady=5)
        self.main_paned.add(right_panel, weight=5)  # 50% width
        
        # Store paned window references for sash positioning
        self.paned_window = self.main_paned  # For compatibility with resize handler
        
        # Set initial sash positions after window is drawn
        self.root.after(100, self.set_initial_sash)
        
        # Build remaining sections (calibration, parameters, batch)
        self.build_calibration_section(middle_inner)
        self.build_parameters_section(middle_inner)
        self.build_batch_section(middle_inner)
        
        # Build right panel (log)
        self.build_log_section(right_inner)
    
    def add_tooltip(self, widget, text):
        """Add a tooltip to a widget"""
        return ToolTip(widget, text)

    def build_acquisition_section(self, parent):
        """Section 0: Oscilloscope Data Acquisition with multi-scope support"""
        frame = ttk.LabelFrame(parent, text="Oscilloscope acquisition", padding=(15, 10))
        frame.pack(fill='x', pady=(0, 15))
        
        # Row 1: Scope type selector and address (full width)
        row1 = ttk.Frame(frame, style='Panel.TFrame')
        row1.pack(fill='x', pady=5)
        
        # Scope type selector
        lbl_scope_type = ttk.Label(row1, text="Scope type:", width=14, anchor='e')
        lbl_scope_type.pack(side='left', padx=(0, 8))
        
        self.scope_type_var = tk.StringVar(value=self.SCOPE_RIGOL)
        self.scope_type_combo = ttk.Combobox(row1, textvariable=self.scope_type_var, 
                                            values=[self.SCOPE_RIGOL, self.SCOPE_LECROY],
                                            state='readonly', width=10)
        self.scope_type_combo.pack(side='left', padx=(0, 15))
        self.scope_type_combo.bind('<<ComboboxSelected>>', self.on_scope_type_changed)
        self.add_tooltip(self.scope_type_combo, "Select oscilloscope type (Rigol or LeCroy)")
        
        lbl = ttk.Label(row1, text="Scope address:", width=14, anchor='e')
        lbl.pack(side='left', padx=(0, 8))
        
        # Set default addresses for both scope types
        self.scope_address_var = tk.StringVar(value="TCPIP::169.254.228.210::INSTR")
        self.scope_address_combo = ttk.Combobox(row1, textvariable=self.scope_address_var, width=35)
        self.scope_address_combo.pack(side='left', fill='x', expand=True, padx=(0, 5))
        
        # Row 2: Test and Refresh buttons (with scope type indicator)
        btn_row = ttk.Frame(frame, style='Panel.TFrame')
        btn_row.pack(fill='x', pady=5)

        # Create a container frame to center the buttons
        btn_container = ttk.Frame(btn_row)
        btn_container.pack(anchor='center')

        test_btn = self.create_button(btn_container, text="Test connection", command=self.test_scope_connection)
        test_btn.pack(side='left', padx=(0, 10))
        self.test_btn = test_btn  # Store reference AFTER creation

        refresh_btn = self.create_button(btn_container, text="Refresh", command=self.refresh_scope_addresses)
        refresh_btn.pack(side='left')
        self.refresh_btn = refresh_btn  # Store reference AFTER creation
        
        # Row 3: Study Name and Run Name (2 columns)
        row3 = ttk.Frame(frame, style='Panel.TFrame')
        row3.pack(fill='x', pady=5)
        
        lbl = ttk.Label(row3, text="Study name:", width=14, anchor='e')
        lbl.pack(side='left', padx=(0, 8))
        self.study_name_var = tk.StringVar(value="Tungsten_Calibration")
        self.study_entry = ttk.Entry(row3, textvariable=self.study_name_var, width=20)
        self.study_entry.pack(side='left', padx=(0, 15))
        
        lbl2 = ttk.Label(row3, text="Run name:", width=10, anchor='e')
        lbl2.pack(side='left', padx=(0, 8))
        self.run_name_var = tk.StringVar(value="spot1")
        self.run_entry = ttk.Entry(row3, textvariable=self.run_name_var, width=20)
        self.run_entry.pack(side='left', fill='x', expand=True)
        
        # Row 4: Operator (shorter width) and Grating (shorter width)
        row4 = ttk.Frame(frame, style='Panel.TFrame')
        row4.pack(fill='x', pady=5)
        
        lbl = ttk.Label(row4, text="Operator:", width=14, anchor='e')
        lbl.pack(side='left', padx=(0, 8))
        self.operator_var = tk.StringVar(value="Angus")
        self.operator_entry = ttk.Entry(row4, textvariable=self.operator_var, width=10)
        self.operator_entry.pack(side='left', padx=(0, 15))
        
        lbl2 = ttk.Label(row4, text="Grating (µm):", width=12, anchor='e')
        lbl2.pack(side='left', padx=(0, 8))
        grating_options = ['1.6', '1.9', '2.2', '2.5', '2.8', '3.1', '3.4', '3.7', 
                        '4.0', '4.2', '4.4', '4.6', '4.9', '5.2', '5.5', '5.8', 
                        '6.4', '7.0', '7.6', '8.2', '8.8', '9.4']
        self.acq_grating_var = tk.StringVar(value="6.4")
        self.grating_combo = ttk.Combobox(row4, textvariable=self.acq_grating_var, 
                                    values=grating_options, width=8)
        self.grating_combo.pack(side='left')
        
        # Row 5: Number of traces (shorter width) and Trigger rate (shorter width)
        row5 = ttk.Frame(frame, style='Panel.TFrame')
        row5.pack(fill='x', pady=5)
        
        lbl = ttk.Label(row5, text="Number of traces:", width=16, anchor='e')
        lbl.pack(side='left', padx=(0, 8))
        self.num_traces_var = tk.StringVar(value="8192")
        self.traces_spin = ttk.Spinbox(row5, from_=1, to=10000, textvariable=self.num_traces_var, width=7)
        self.traces_spin.pack(side='left', padx=(0, 15))
        
        lbl2 = ttk.Label(row5, text="Trigger rate (kHz):", width=16, anchor='e')
        lbl2.pack(side='left', padx=(0, 8))
        self.trigger_rate_var = tk.StringVar(value="1")
        self.trigger_spin = ttk.Spinbox(row5, from_=0.1, to=100, textvariable=self.trigger_rate_var, width=5, increment=0.1)
        self.trigger_spin.pack(side='left')
        
        # Row 6: Data directory
        row6 = ttk.Frame(frame, style='Panel.TFrame')
        row6.pack(fill='x', pady=5)
        
        lbl = ttk.Label(row6, text="Data directory:", width=14, anchor='e')
        lbl.pack(side='left', padx=(0, 8))
        self.data_dir_var = tk.StringVar(value=r"C:\Users\short\Documents\Data")
        self.data_dir_entry = ttk.Entry(row6, textvariable=self.data_dir_var, width=25)
        self.data_dir_entry.pack(side='left', fill='x', expand=True, padx=(0, 5))
        
        self.browse_button = self.create_button(row6, text="Browse...", command=self.browse_data_directory)
        self.browse_button.pack(side='left')
        
        # Separator
        ttk.Separator(frame, orient='horizontal').pack(fill='x', pady=5)

        # Row 7: Main action buttons (Acquire data and Acquire for calibration)
        acq_row1 = ttk.Frame(frame, style='Panel.TFrame')
        acq_row1.pack(fill='x', pady=5)

        # Create a container frame to center the buttons
        acq_container1 = ttk.Frame(acq_row1)
        acq_container1.pack(anchor='center')

        # Create RED "Acquire data" button with explicit styling
        self.acquire_button = tk.Button(
            acq_container1, 
            text="Acquire data", 
            command=self.acquire_rigol_data,
            bg='#9B2C2C',  # Muted brick red for dark theme
            fg='white',
            activebackground='#7A2323',
            activeforeground='white',
            relief=tk.RAISED,  # Changed from FLAT to see if theme is forcing flat
            bd=1,
            padx=20, 
            pady=8,
            font=("Arial", 10, 'bold'),
            cursor="hand2",
            highlightthickness=0,
            highlightbackground='#d32f2f',  # Force highlight color
            highlightcolor='#d32f2f'
        )
        self.acquire_button.pack(side='left', padx=(0, 10))

        # Force update colors after creation
        self.acquire_button.configure(bg='#9B2C2C', fg='white')
        self.acquire_button.update_idletasks()

        # Add hover effects
        def on_acquire_enter(e):
            if self.acquire_button['state'] != 'disabled':
                self.acquire_button.config(bg='#7A2323')
                #self.log_message(f"[DEBUG] Button hover - bg changed to #f44336")

        def on_acquire_leave(e):
            if self.acquire_button['state'] != 'disabled':
                self.acquire_button.config(bg='#9B2C2C')
                #self.log_message(f"[DEBUG] Button leave - bg changed back to #d32f2f")

        self.acquire_button.bind("<Enter>", on_acquire_enter)
        self.acquire_button.bind("<Leave>", on_acquire_leave)

        # Add tooltip separately
        self.add_tooltip(self.acquire_button, "Acquire data from oscilloscope")

        # Standard button for calibration
        self.acquire_calib_button = self.create_button(
            acq_container1, 
            text="Acquire for calibration", 
            command=self.acquire_for_calibration
        )
        self.acquire_calib_button.pack(side='left')

        # Row 8: Secondary action buttons (Acquire baseline and Stop)
        acq_row2 = ttk.Frame(frame, style='Panel.TFrame')
        acq_row2.pack(fill='x', pady=5)

        # Create a container frame to center the buttons
        acq_container2 = ttk.Frame(acq_row2)
        acq_container2.pack(anchor='center')

        self.acquire_baseline_button = self.create_button(acq_container2, text="Acquire baseline", command=self.acquire_for_baseline)
        self.acquire_baseline_button.pack(side='left', padx=(0, 10))

        self.stop_acq_button = self.create_button(acq_container2, text="Stop", command=self.stop_continuous_acquisition, state='normal')
        self.stop_acq_button.pack(side='left')

        # Row 9: Continuous acquisition and Auto-fit checkboxes (centered)
        check_row = ttk.Frame(frame, style='Panel.TFrame')
        check_row.pack(fill='x', pady=5)

        # Create a container frame to center the checkboxes
        check_container = ttk.Frame(check_row)
        check_container.pack(anchor='center')

        self.continuous_acq_var = tk.BooleanVar(value=False)
        self.continuous_checkbox = ttk.Checkbutton(check_container, text="Continuous Acquisition", variable=self.continuous_acq_var)
        self.continuous_checkbox.pack(side='left', padx=(0, 20))

        self.autofit_var = tk.BooleanVar(value=True)
        self.autofit_checkbox = ttk.Checkbutton(check_container, text="Auto-fit after acquisition", variable=self.autofit_var)
        self.autofit_checkbox.pack(side='left')

        # Status label
        self.acq_status_var = tk.StringVar(value="Ready")
        status_lbl = ttk.Label(frame, textvariable=self.acq_status_var, foreground='#888888')
        status_lbl.pack(pady=5)
        
        # Add tooltips
        self.add_tooltip(self.scope_type_combo, "Select oscilloscope type (Rigol or LeCroy)")
        self.add_tooltip(test_btn, "Test connection to the oscilloscope")
        self.add_tooltip(refresh_btn, "Scan for available oscilloscope addresses")
        self.add_tooltip(self.study_entry, "Study name - creates a folder with this name")
        self.add_tooltip(self.run_entry, "Run name - used in filename")
        self.add_tooltip(self.operator_entry, "Operator name")
        self.add_tooltip(self.grating_combo, "Grating spacing in micrometers (µm)")
        self.add_tooltip(self.traces_spin, "Number of waveforms to average")
        self.add_tooltip(self.trigger_spin, "Trigger rate in kHz")
        self.add_tooltip(self.data_dir_entry, "Directory where data files will be saved")
        self.add_tooltip(self.browse_button, "Browse to select data directory")
        self.add_tooltip(self.acquire_button, "Acquire data from oscilloscope")
        self.add_tooltip(self.acquire_calib_button, "Acquire data and use it for grating spacing calibration")
        self.add_tooltip(self.stop_acq_button, "Stop continuous acquisition")
        self.add_tooltip(self.continuous_checkbox, "Continuously acquire data until Stop button is pressed")
        self.add_tooltip(self.autofit_checkbox, "Automatically fit and display results after each acquisition")
        self.add_tooltip(self.acquire_baseline_button, "Acquire data and use it as baseline reference for future fits")

    def debug_read_lecroy_raw(self):
        """Debug method to read raw data from LeCroy and save to files"""
        scope_address = self.scope_address_var.get().strip()
        if not scope_address:
            self.log_message("No scope address configured", logging.ERROR)
            return
        
        def debug_thread():
            try:
                import pyvisa
                import time
                from pathlib import Path
                from datetime import datetime
                
                self.log_message("=" * 60)
                self.log_message("DEBUG: Reading raw data from LeCroy")
                self.log_message("=" * 60)
                
                rm = pyvisa.ResourceManager()
                scope = rm.open_resource(scope_address)
                scope.timeout = 10000  # 10 second timeout
                
                # Configure for reading
                scope.read_termination = '\n'
                scope.write_termination = '\n'
                
                # Test basic communication
                idn = scope.query("*IDN?").strip()
                self.log_message(f"IDN: {idn}")
                
                # Try different binary configurations
                self.log_message("\n--- Testing different binary modes ---")
                
                # Method 1: Simple WAVEFORM? command
                self.log_message("\n--- Method 1: C2:WAVEFORM? ---")
                try:
                    scope.write("CHDR OFF")
                    time.sleep(0.1)
                    scope.write("CFMT DEF9,WORD,BIN")
                    time.sleep(0.1)
                    
                    data = scope.query_binary_values("C2:WAVEFORM?", datatype='h', is_big_endian=True)
                    self.log_message(f"Got {len(data)} samples from C2:WAVEFORM?")
                    if len(data) > 0:
                        self.log_message(f"  Min: {min(data)}, Max: {max(data)}, Mean: {sum(data)/len(data):.2f}")
                        
                        # Save raw data
                        debug_dir = Path("debug_lecroy")
                        debug_dir.mkdir(exist_ok=True)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = debug_dir / f"{timestamp}_C2_waveform_raw.txt"
                        with open(filename, 'w') as f:
                            for i, val in enumerate(data[:1000]):  # First 1000 samples
                                f.write(f"{i}\t{val}\n")
                        self.log_message(f"Saved first 1000 samples to: {filename}")
                except Exception as e:
                    self.log_message(f"Method 1 failed: {e}")
                
                # Method 2: Try with ASC format to see what we get
                self.log_message("\n--- Method 2: C2:WAVEFORM? ASC ---")
                try:
                    scope.write("CFMT DEF9,WORD,ASC")
                    time.sleep(0.1)
                    asc_data = scope.query("C2:WAVEFORM?")
                    self.log_message(f"ASC response (first 500 chars): {asc_data[:500]}")
                    
                    # Try to parse if it's comma-separated
                    if ',' in asc_data:
                        values = [float(x.strip()) for x in asc_data.split(',') if x.strip()]
                        self.log_message(f"Parsed {len(values)} comma-separated values")
                        if len(values) > 0:
                            self.log_message(f"  Min: {min(values):.4f}, Max: {max(values):.4f}")
                except Exception as e:
                    self.log_message(f"Method 2 failed: {e}")
                
                # Method 3: Try WAVEFORM_SETUP? to see settings
                self.log_message("\n--- Method 3: Querying scope settings ---")
                try:
                    scope.write("CFMT DEF9,WORD,BIN")  # Back to binary
                    wf_setup = scope.query("WAVEFORM_SETUP?")
                    self.log_message(f"WAVEFORM_SETUP: {wf_setup[:200]}")
                except Exception as e:
                    self.log_message(f"Failed: {e}")
                
                # Method 4: Get timebase info
                self.log_message("\n--- Method 4: Timebase info ---")
                try:
                    tdiv = scope.query("TDIV?")
                    trdl = scope.query("TRDL?")
                    self.log_message(f"TDIV: {tdiv.strip()}")
                    self.log_message(f"TRDL: {trdl.strip()}")
                except Exception as e:
                    self.log_message(f"Failed: {e}")
                
                # Method 5: Try INSPECT? command (LeCroy specific)
                self.log_message("\n--- Method 5: C2:INSPECT? ---")
                try:
                    inspect = scope.query("C2:INSPECT?")
                    self.log_message(f"INSPECT response (first 500 chars): {inspect[:500]}")
                except Exception as e:
                    self.log_message(f"Failed: {e}")
                
                scope.close()
                rm.close()
                
                self.log_message("\n" + "=" * 60)
                self.log_message("DEBUG complete. Check debug_lecroy/ folder for files.")
                self.log_message("=" * 60)
                
            except Exception as e:
                self.log_message(f"Debug read failed: {e}", logging.ERROR)
                import traceback
                self.log_message(traceback.format_exc(), logging.DEBUG)
        
        # Start debug in separate thread to prevent GUI freeze
        thread = threading.Thread(target=debug_thread, daemon=True)
        thread.start()

    def on_scope_type_changed(self, event=None):
        """Handle scope type selection change"""
        new_type = self.scope_type_var.get()
        self.current_scope_type = new_type
        
        # Set default address based on scope type
        if new_type == self.SCOPE_RIGOL:
            if not self.scope_address_var.get().strip() or "GPIB" in self.scope_address_var.get().strip() or "inst0" in self.scope_address_var.get().strip():
                self.scope_address_var.set("TCPIP::169.254.228.210::INSTR")
        else:  # LeCroy
            if not self.scope_address_var.get().strip() or "TCPIP::" in self.scope_address_var.get().strip():
                self.scope_address_var.set("TCPIP::169.254.214.24::INSTR")
        
        # Update the acquire button text based on scope type
        self.acquire_button.config(text="Acquire data")
        
        self.log_message(f"Switched to {new_type} oscilloscope")
        self.save_preferences()
        self.test_scope_connection()

    def setup_preference_saving(self):
        """Setup traces to save preferences when variables change"""
        vars_to_monitor = [
            self.scope_address_var,
            self.data_dir_var,
            self.study_name_var,
            self.run_name_var,
            self.operator_var,
            self.acq_grating_var,
            self.num_traces_var,
            self.trigger_rate_var,
            self.scope_type_var,
        ]
        
        for var in vars_to_monitor:
            var.trace_add('write', lambda *args: self.save_preferences())
        
        # IMPORTANT: DO NOT sync acq_grating_var with config
        # The acquisition grating and calibration grating are independent
        
        self.continuous_acq_var.trace_add('write', lambda *args: self.save_preferences())
        self.autofit_var.trace_add('write', lambda *args: self.save_preferences())

    def _update_calibration_from_acquisition(self, grating_spacing_um, frequency):
        """Update the calibration display and config with the new grating spacing"""
        # Update the grating edit field (calibration pane)
        self.grating_edit.delete(0, tk.END)
        self.grating_edit.insert(0, f"{grating_spacing_um:.6f}")
        
        # DO NOT update the acquisition combobox - they are independent
        
        # Update the config
        self.config['tgs']['grating_spacing'] = grating_spacing_um
        
        # Save to config file
        self.save_config()
        
        # Store the calibrated spacing
        self.calibrated_spacing = grating_spacing_um
        
        # Log the result
        self.log_message(f"Calibration complete: f = {frequency/1e6:.3f} MHz, grating = {grating_spacing_um:.4f} µm")

    def build_results_summary_section(self, parent):
        """Section: Results Summary - plot of thermal diffusivity and SAW speed"""
        frame = ttk.LabelFrame(parent, text="Results Summary", padding=(10, 8))
        frame.pack(fill='both', expand=True)
        
        # Create title bar with save button
        title_bar = ttk.Frame(frame)
        title_bar.pack(fill='x', pady=(0, 5))
        
        title_label = ttk.Label(title_bar, text="Thermal Diffusivity & SAW Speed vs Run Index", 
                            font=('Arial', 9, 'bold'))
        title_label.pack(side='left', padx=(5, 0))
        
        save_btn = self.create_button(title_bar, text="Save plot", command=self.save_summary_plot)
        save_btn.pack(side='right', padx=(0, 5))
        self.add_tooltip(save_btn, "Save the summary plot as PNG, PDF, or SVG")
        
        # Create matplotlib figure for summary plot
        self.summary_fig = Figure(figsize=(5, 3), dpi=100, facecolor=self.panel_bg, tight_layout=True)
        
        # Create twin axes for dual Y-axis
        self.summary_ax = self.summary_fig.add_subplot(111)
        self.summary_ax.set_facecolor(self.panel_bg)
        self.summary_ax2 = self.summary_ax.twinx()
        self.summary_ax2.set_facecolor(self.panel_bg)
        
        # Style the axes
        for ax in [self.summary_ax, self.summary_ax2]:
            ax.tick_params(colors=self.fg_color, labelsize=8)
            ax.xaxis.label.set_color(self.fg_color)
            for spine in ax.spines.values():
                spine.set_color(self.fg_color)
        
        # Set colors for axes
        self.summary_ax.set_ylabel('Thermal Diffusivity [mm²/s]', color='#ff6b6b', fontsize=9)
        self.summary_ax2.set_ylabel('SAW Speed [m/s]', color='#4dabf7', fontsize=9)
        self.summary_ax.set_xlabel('Run Index', fontsize=9)
        
        # Set tick colors
        self.summary_ax.tick_params(axis='y', labelcolor='#ff6b6b')
        self.summary_ax2.tick_params(axis='y', labelcolor='#4dabf7')
        
        # Initialize data storage
        self.summary_run_indices = []
        self.summary_thermal_diffusivity = []
        self.summary_saw_speed = []
        
        # Create plot lines (empty initially)
        self.summary_line1 = None
        self.summary_line2 = None
        self.summary_scatter1 = None
        self.summary_scatter2 = None
        
        self.summary_canvas = FigureCanvasTkAgg(self.summary_fig, master=frame)
        self.summary_canvas.get_tk_widget().pack(fill='both', expand=True, padx=5, pady=5)
        
        self.summary_canvas.draw_idle()
        
        # Initial placeholder text
        self._show_summary_placeholder()

    def _show_summary_placeholder(self):
        """Show placeholder text when no data is available"""
        self.summary_ax.clear()
        self.summary_ax2.clear()
        
        self.summary_ax.text(0.5, 0.5, 'No data available\nAdd runs to the batch queue',
                            ha='center', va='center', transform=self.summary_ax.transAxes,
                            color=self.fg_color, fontsize=10, fontfamily='Arial')
        self.summary_ax.set_xlim(0, 1)
        self.summary_ax.set_ylim(0, 1)
        self.summary_ax.set_xticks([])
        self.summary_ax.set_yticks([])
        self.summary_ax2.set_xticks([])
        self.summary_ax2.set_yticks([])
        
        self.summary_fig.tight_layout()
        self.summary_canvas.draw_idle()

    def update_summary_plot(self):
        """Update the summary plot with current fit parameters"""
        # Check if we have data to plot
        if not self.pos_files or not self.file_to_fit_params:
            self._show_summary_placeholder()
            return
        
        # Clear axes
        self.summary_ax.clear()
        self.summary_ax2.clear()
        
        # Collect data from processed runs
        run_indices = []
        thermal_diffusivity = []
        thermal_diffusivity_err = []
        saw_speed = []
        saw_speed_err = []
        
        # Get grating spacing once
        grating_spacing = self.config.get('tgs', {}).get('grating_spacing', self.calibrated_spacing or 3.5276)
        
        for i, pos_file in enumerate(self.pos_files, 1):
            file_id = Path(pos_file).stem
            
            if file_id in self.file_to_fit_params:
                params = self.file_to_fit_params[file_id]
                
                # Get thermal diffusivity (alpha) - convert from m²/s to mm²/s
                alpha = params.get('alpha')
                alpha_err = params.get('alpha_err')
                
                # Always append a value, even if it's None
                if alpha is not None and not np.isnan(alpha) and not np.isinf(alpha):
                    thermal_diffusivity.append(alpha * 1e6)
                    # Only include error if valid, otherwise use 0
                    if alpha_err is not None and not np.isnan(alpha_err) and not np.isinf(alpha_err):
                        thermal_diffusivity_err.append(alpha_err * 1e6)
                    else:
                        thermal_diffusivity_err.append(0.0)
                else:
                    thermal_diffusivity.append(None)
                    thermal_diffusivity_err.append(None)
                
                # Get SAW frequency
                f_hz = params.get('f')
                f_err = params.get('f_err')
                if f_hz is not None and not np.isnan(f_hz) and not np.isinf(f_hz):
                    saw_speed_value = f_hz * (grating_spacing * 1e-6)
                    saw_speed.append(saw_speed_value)
                    if f_err is not None and not np.isnan(f_err) and not np.isinf(f_err):
                        saw_speed_err.append(f_err * (grating_spacing * 1e-6))
                    else:
                        saw_speed_err.append(0.0)
                else:
                    saw_speed.append(None)
                    saw_speed_err.append(None)
                
                run_indices.append(i)
            else:
                run_indices.append(i)
                thermal_diffusivity.append(None)
                thermal_diffusivity_err.append(None)
                saw_speed.append(None)
                saw_speed_err.append(None)
        
        # Check if we have any valid data to plot
        has_thermal = any(v is not None for v in thermal_diffusivity)
        has_saw = any(v is not None for v in saw_speed)
        
        if not has_thermal and not has_saw:
            self._show_summary_placeholder()
            return
        
        # Convert to numpy arrays
        run_indices = np.array(run_indices)
        thermal_data = np.array(thermal_diffusivity, dtype=float)
        thermal_err_data = np.array(thermal_diffusivity_err, dtype=float)
        saw_data = np.array(saw_speed, dtype=float)
        saw_err_data = np.array(saw_speed_err, dtype=float)
        
        # Filter valid thermal data (exclude None/NaN/Inf)
        valid_thermal = ~np.isnan(thermal_data) & ~np.isinf(thermal_data)
        
        if np.any(valid_thermal):
            thermal_indices = run_indices[valid_thermal]
            thermal_values = thermal_data[valid_thermal]
            thermal_err_values = thermal_err_data[valid_thermal]
            
            # For errors: only exclude if they are NaN or Inf (0 is valid - means no error bars)
            valid_err_thermal = ~np.isnan(thermal_err_values) & ~np.isinf(thermal_err_values)
            
            # Use all valid points, even if error is 0
            thermal_indices_final = thermal_indices[valid_err_thermal] if np.any(valid_err_thermal) else thermal_indices
            thermal_values_final = thermal_values[valid_err_thermal] if np.any(valid_err_thermal) else thermal_values
            thermal_err_final = thermal_err_values[valid_err_thermal] if np.any(valid_err_thermal) else np.zeros_like(thermal_values)
            
            if len(thermal_indices_final) > 0:
                # Plot with error bars
                self.summary_ax.errorbar(thermal_indices_final, thermal_values_final, 
                                        yerr=thermal_err_final,
                                        color='#ff6b6b', marker='o', linestyle='none', 
                                        markersize=2, capsize=2, capthick=1,
                                        elinewidth=1, zorder=5, alpha=0.7,
                                        label='Thermal Diffusivity')
        
        # Filter valid SAW data
        valid_saw = ~np.isnan(saw_data) & ~np.isinf(saw_data)
        
        if np.any(valid_saw):
            saw_indices = run_indices[valid_saw]
            saw_values = saw_data[valid_saw]
            saw_err_values = saw_err_data[valid_saw]
            
            # For errors: only exclude if they are NaN or Inf
            valid_err_saw = ~np.isnan(saw_err_values) & ~np.isinf(saw_err_values)
            
            saw_indices_final = saw_indices[valid_err_saw] if np.any(valid_err_saw) else saw_indices
            saw_values_final = saw_values[valid_err_saw] if np.any(valid_err_saw) else saw_values
            saw_err_final = saw_err_values[valid_err_saw] if np.any(valid_err_saw) else np.zeros_like(saw_values)
            
            if len(saw_indices_final) > 0:
                saw_indices_offset = saw_indices_final + 0.15
                self.summary_ax2.errorbar(saw_indices_offset, saw_values_final, 
                                        yerr=saw_err_final,
                                        color='#4dabf7', marker='s', linestyle='none', 
                                        markersize=2, capsize=2, capthick=1,
                                        elinewidth=1, zorder=5, alpha=0.7,
                                        label='SAW Speed')
        
        # Set labels and styling
        self.summary_ax.set_xlabel('Run Index', fontsize=9, fontfamily='Arial', color=self.fg_color)
        self.summary_ax.set_ylabel('Thermal Diffusivity [mm²/s]', color='#ff6b6b', fontsize=9, fontfamily='Arial')
        self.summary_ax2.set_ylabel('SAW Speed [m/s]', color='#4dabf7', fontsize=9, fontfamily='Arial')
        self.summary_ax2.yaxis.set_label_coords(1.25, 0.5)
        
        # Set tick colors and fonts
        self.summary_ax.tick_params(axis='y', labelcolor='#ff6b6b', colors=self.fg_color, labelsize=8)
        self.summary_ax2.tick_params(axis='y', labelcolor='#4dabf7', colors=self.fg_color, labelsize=8)
        self.summary_ax.tick_params(axis='x', colors=self.fg_color, labelsize=8)
        
        # Set tick label font to Arial
        for label in self.summary_ax.get_xticklabels():
            label.set_fontfamily('Arial')
            label.set_fontsize(8)
        for label in self.summary_ax.get_yticklabels():
            label.set_fontfamily('Arial')
            label.set_fontsize(8)
        for label in self.summary_ax2.get_yticklabels():
            label.set_fontfamily('Arial')
            label.set_fontsize(8)
        
        # Dynamic x-axis tick spacing
        num_runs = len(run_indices)
        if num_runs > 0:
            self.summary_ax.set_xlim(0.5, max(run_indices) + 0.5)
            
            if num_runs <= 10:
                tick_spacing = 1
            elif num_runs <= 20:
                tick_spacing = 2
            elif num_runs <= 50:
                tick_spacing = 5
            elif num_runs <= 100:
                tick_spacing = 10
            elif num_runs <= 200:
                tick_spacing = 20
            elif num_runs <= 500:
                tick_spacing = 50
            else:
                tick_spacing = 100
            
            ticks = np.arange(1, max(run_indices) + 1, tick_spacing)
            self.summary_ax.set_xticks(ticks)
            
            if tick_spacing <= 2 and num_runs > 15:
                for label in self.summary_ax.get_xticklabels():
                    label.set_rotation(45)
                    label.set_horizontalalignment('right')
        
        # Set background colors
        self.summary_ax.set_facecolor(self.panel_bg)
        self.summary_ax2.set_facecolor(self.panel_bg)
        
        # Style spines
        for spine in self.summary_ax.spines.values():
            spine.set_color(self.fg_color)
        for spine in self.summary_ax2.spines.values():
            spine.set_color(self.fg_color)
        
        # Adjust layout
        self.summary_fig.tight_layout()
        self.summary_fig.subplots_adjust(right=0.82)
        
        # Force immediate redraw
        self.summary_canvas.draw()
        self.summary_canvas.flush_events()

    def save_summary_plot(self):
        """Save the current summary plot to a file"""
        if not self.pos_files or not self.file_to_fit_params:
            self.log_message("No data to save. Please process some runs first.", logging.WARNING)
            return
        
        # Ask user for save location
        file_path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[
                ("PNG image", "*.png"),
                ("PDF document", "*.pdf"),
                ("SVG image", "*.svg"),
                ("All files", "*.*")
            ],
            title="Save summary plot as"
        )
        
        if not file_path:
            return
        
        try:
            # Create a new figure for saving with higher DPI
            save_fig = Figure(figsize=(8, 4), dpi=300, facecolor=self.panel_bg, tight_layout=True)
            save_ax = save_fig.add_subplot(111)
            save_ax2 = save_ax.twinx()
            
            # Style the axes
            for ax in [save_ax, save_ax2]:
                ax.set_facecolor(self.panel_bg)
                ax.tick_params(colors=self.fg_color, labelsize=9)
                for spine in ax.spines.values():
                    spine.set_color(self.fg_color)
            
            # Set tick label font to Arial
            for ax in [save_ax, save_ax2]:
                for label in ax.get_xticklabels():
                    label.set_fontfamily('Arial')
                    label.set_fontsize(9)
                for label in ax.get_yticklabels():
                    label.set_fontfamily('Arial')
                    label.set_fontsize(9)
            
            # Collect data from processed runs
            run_indices = []
            thermal_diffusivity = []
            thermal_diffusivity_err = []
            saw_speed = []
            saw_speed_err = []
            
            grating_spacing = self.config.get('tgs', {}).get('grating_spacing', self.calibrated_spacing or 3.5276)
            
            for i, pos_file in enumerate(self.pos_files, 1):
                file_id = Path(pos_file).stem
                if file_id in self.file_to_fit_params:
                    params = self.file_to_fit_params[file_id]
                    
                    alpha = params.get('alpha')
                    alpha_err = params.get('alpha_err')
                    if alpha is not None and not np.isnan(alpha) and not np.isinf(alpha):
                        thermal_diffusivity.append(alpha * 1e6)
                        if alpha_err is not None and not np.isnan(alpha_err) and not np.isinf(alpha_err):
                            thermal_diffusivity_err.append(alpha_err * 1e6)
                        else:
                            thermal_diffusivity_err.append(None)
                    else:
                        thermal_diffusivity.append(None)
                        thermal_diffusivity_err.append(None)
                    
                    f_hz = params.get('f')
                    f_err = params.get('f_err')
                    
                    if f_hz is not None and not np.isnan(f_hz) and not np.isinf(f_hz):
                        saw_speed_value = f_hz * (grating_spacing * 1e-6)
                        saw_speed.append(saw_speed_value)
                        if f_err is not None and not np.isnan(f_err) and not np.isinf(f_err):
                            saw_speed_err.append(f_err * (grating_spacing * 1e-6))
                        else:
                            saw_speed_err.append(None)
                    else:
                        saw_speed.append(None)
                        saw_speed_err.append(None)
                    
                    run_indices.append(i)
                else:
                    run_indices.append(i)
                    thermal_diffusivity.append(None)
                    thermal_diffusivity_err.append(None)
                    saw_speed.append(None)
                    saw_speed_err.append(None)
            
            if not run_indices:
                raise ValueError("No valid data to plot")
            
            # Convert to numpy arrays
            run_indices = np.array(run_indices)
            thermal_data = np.array(thermal_diffusivity)
            thermal_err_data = np.array(thermal_diffusivity_err)
            saw_data = np.array(saw_speed)
            saw_err_data = np.array(saw_speed_err)
            
            # Filter valid data
            valid_thermal = ~np.isnan(thermal_data) & ~np.isinf(thermal_data)
            valid_saw = ~np.isnan(saw_data) & ~np.isinf(saw_data)
            
            # Get point size from config
            point_size = self.config.get('plot', {}).get('summary_point_size', 40)
            
            if np.any(valid_thermal):
                thermal_indices = run_indices[valid_thermal]
                thermal_values = thermal_data[valid_thermal]
                thermal_err_values = thermal_err_data[valid_thermal]
                
                # Filter out points where error is inf or NaN - EXCLUDE them entirely
                valid_err_thermal = ~np.isnan(thermal_err_values) & ~np.isinf(thermal_err_values)
                thermal_indices = thermal_indices[valid_err_thermal]
                thermal_values = thermal_values[valid_err_thermal]
                thermal_err_values = thermal_err_values[valid_err_thermal]
                
                if len(thermal_indices) > 0:
                    save_ax.errorbar(thermal_indices, thermal_values, 
                                    yerr=thermal_err_values,
                                    color='#ff6b6b', marker='o', linestyle='none', 
                                    markersize=2, capsize=2, capthick=1,
                                    elinewidth=1, zorder=5)

            if np.any(valid_saw):
                saw_indices = run_indices[valid_saw]
                saw_values = saw_data[valid_saw]
                saw_err_values = saw_err_data[valid_saw]
                
                # Filter out points where error is inf or NaN - EXCLUDE them entirely
                valid_err_saw = ~np.isnan(saw_err_values) & ~np.isinf(saw_err_values)
                saw_indices = saw_indices[valid_err_saw]
                saw_values = saw_values[valid_err_saw]
                saw_err_values = saw_err_values[valid_err_saw]
                
                if len(saw_indices) > 0:
                    saw_indices_offset = saw_indices + 0.15
                    save_ax2.errorbar(saw_indices_offset, saw_values, 
                                    yerr=saw_err_values,
                                    color='#4dabf7', marker='s', linestyle='none', 
                                    markersize=2, capsize=2, capthick=1,
                                    elinewidth=1, zorder=5)
                
            # Set labels
            save_ax.set_xlabel('Run Index', fontsize=10, fontfamily='Arial', color=self.fg_color)
            save_ax.set_ylabel('Thermal Diffusivity [mm²/s]', color='#ff6b6b', fontsize=10, fontfamily='Arial')
            save_ax2.set_ylabel('SAW Speed [m/s]', color='#4dabf7', fontsize=10, fontfamily='Arial')
                
            # Set tick colors
            save_ax.tick_params(axis='y', labelcolor='#ff6b6b')
            save_ax2.tick_params(axis='y', labelcolor='#4dabf7')
                
            # Dynamic x-axis tick spacing
            num_runs = len(run_indices)
            if num_runs > 0:
                save_ax.set_xlim(0.5, max(run_indices) + 0.5)
                    
                if num_runs <= 10:
                    tick_spacing = 1
                elif num_runs <= 20:
                    tick_spacing = 2
                elif num_runs <= 50:
                    tick_spacing = 5
                elif num_runs <= 100:
                    tick_spacing = 10
                elif num_runs <= 200:
                    tick_spacing = 20
                elif num_runs <= 500:
                    tick_spacing = 50
                else:
                    tick_spacing = 100
                    
                ticks = np.arange(1, max(run_indices) + 1, tick_spacing)
                save_ax.set_xticks(ticks)
                
            # NO grid lines
            # save_ax.grid(True, linestyle='--', linewidth=0.5, alpha=0.3, color=self.fg_color)
                
            # Save the figure
            save_fig.savefig(file_path, dpi=300, bbox_inches='tight', facecolor=self.panel_bg)
            plt.close(save_fig)
                
            self.log_message(f"Summary plot saved to: {file_path}")
            self.log_message(f"Summary plot saved successfully to:\n{file_path}", logging.WARNING)
                
        except Exception as e:
            self.log_message(f"Error saving summary plot: {str(e)}", logging.ERROR)
            messagebox.showerror("Error", f"Failed to save summary plot:\n{str(e)}")

    def refresh_scope_addresses(self):
        """Refresh the dropdown list of available scope addresses"""
        if hasattr(self, 'shutdown_flag') and self.shutdown_flag:
            return
        
        def scan_thread():
            if hasattr(self, 'shutdown_flag') and self.shutdown_flag:
                return
            resources = self.scan_available_resources()
            
            def update_dropdown():
                if hasattr(self, 'shutdown_flag') and self.shutdown_flag:
                    return
                if resources:
                    # Keep the current value if it's in the list
                    current = self.scope_address_var.get()
                    
                    # Update the combobox values
                    self.scope_address_combo['values'] = resources
                    
                    # If current is not in resources and resources exist, optionally select first
                    if current not in resources and resources:
                        # Don't auto-change - let user decide
                        pass
                else:
                    self.scope_address_combo['values'] = []
                    # Provide helpful default addresses based on scope type
                    if self.current_scope_type == self.SCOPE_RIGOL:
                        default_addrs = ["TCPIP::169.254.228.210::INSTR", "USB0::0x1AB1::0x04CE::DS5Z000000001::INSTR"]
                    else:
                        default_addrs = ["TCPIP::169.254.214.24::INSTR", "GPIB0::1::INSTR"]
                    self.scope_address_combo['values'] = default_addrs
                    self.log_message("No VISA resources found. Make sure the scope is connected and PyVISA is configured properly.", logging.WARNING)
            
            self.root.after(0, update_dropdown)
        
        thread = threading.Thread(target=scan_thread)
        thread.daemon = True
        thread.start()

    def stop_continuous_acquisition(self):
        """Stop continuous acquisition mode and restore scope settings"""
        
        if hasattr(self, 'continuous_acq_active') and self.continuous_acq_active:
            self.continuous_acq_active = False
            
            # Restore scope settings if we have the connection info
            if self.scope_connection_string and self.orig_scale and self.orig_offset:
                try:
                    import pyvisa
                    rm = pyvisa.ResourceManager()
                    scope = rm.open_resource(self.scope_connection_string)
                    scope.timeout = 5000
                    
                    if self.current_scope_type == self.SCOPE_RIGOL:
                        scope.write(f":TIMebase:MAIN:SCALe {self.orig_scale:.10g}")
                        scope.write(f":TIMebase:MAIN:OFFSet {self.orig_offset:.10g}")
                        scope.write(":ACQuire:AVERages 512")
                        scope.write(":RUN")
                    else:  # LeCroy
                        # Use correct LeCroy commands - NO RUN
                        scope.write(f"TDIV {self.orig_scale:.10g}")
                        scope.write(f"TRDL {self.orig_offset:.10g}")
                        scope.write("ACQUIRE NORMAL")  # Back to normal mode
                        # LeCroy scopes are always running
                    
                    scope.close()
                    rm.close()
                    self.log_message("Oscilloscope settings restored")
                except Exception as e:
                    self.log_message(f"Could not restore scope settings: {str(e)}", logging.WARNING)
            
            self.stop_acq_button.config(text="Stopping...", state='normal')
            self.acq_status_var.set("Stopping...")
            self.log_message("Stop button pressed - stopping continuous acquisition...")
            self.root.update_idletasks()
        else:
            self.log_message("No continuous acquisition in progress", logging.WARNING)

    def browse_data_directory(self):
        """Browse for data directory"""
        directory = filedialog.askdirectory(title="Select Data Directory")
        if directory:
            self.data_dir_var.set(directory)
            self.save_preferences()  # Add this line

    def _continuous_acquisition_complete(self):
        """Handle completion of continuous acquisition"""
        self.continuous_acq_active = False
        if hasattr(self, 'stop_acq_event'):
            self.stop_acq_event.clear()
        self.stop_acq_button.config(text="Stop")
        self.acq_status_var.set("Ready")
        self.log_message("Continuous acquisition stopped")

    def acquire_rigol_data(self):
        """Acquire data from oscilloscope (Rigol or LeCroy based on selection)"""
        # Check if continuous acquisition is already running
        if hasattr(self, 'continuous_acq_active') and self.continuous_acq_active:
            self.log_message("Continuous acquisition is already running. Press Stop first.", logging.WARNING)
            return
        
        # Validate inputs
        study_name = self.study_name_var.get().strip()
        if not study_name:
            self.log_message("Please enter a study name", logging.WARNING)
            return
        
        run_name = self.run_name_var.get().strip()
        if not run_name:
            self.log_message("Please enter a run name", logging.WARNING)
            return
        
        operator = self.operator_var.get().strip()
        if not operator:
            self.log_message("Please enter an operator name", logging.WARNING)
            return
        
        try:
            num_traces = int(self.num_traces_var.get())
            if num_traces < 1:
                raise ValueError
        except ValueError:
            self.log_message("Number of traces must be a positive integer", logging.WARNING)
            return
        
        try:
            trigger_rate = float(self.trigger_rate_var.get())
        except ValueError:
            self.log_message("Trigger rate must be a number", logging.WARNING)
            return
        
        try:
            grating = float(self.acq_grating_var.get())
        except ValueError:
            self.log_message("Grating spacing must be a number", logging.WARNING)
            return
        
        scope_address = self.scope_address_var.get().strip()
        data_dir = self.data_dir_var.get().strip()
        
        if not data_dir:
            data_dir = r"C:\Users\short\Documents\Data"
        
        autofit = self.autofit_var.get()
        continuous = self.continuous_acq_var.get()
        
        if continuous:
            self.continuous_acq_active = True

            # Calculate acquisition time
            self.set_ui_state('acquiring')
            if self.current_scope_type == self.SCOPE_RIGOL:
                acq_time = (num_traces / (trigger_rate * 1e3)) * 1.25 + 5
            else:  # LeCroy
                acq_time = (num_traces / (trigger_rate * 1e3)) * 1.1 * 5
            
            self.log_message(f"Continuous acquisition ({self.current_scope_type}): {study_name}/{run_name}, {num_traces} traces, {grating}µm grating")
            self.log_message(f"Estimated {acq_time:.1f}s per acquisition")
            
            # Start continuous acquisition in a separate thread
            #self.acquire_button.config(state='disabled')
            #self.stop_acq_button.config(state='normal')
            self.continuous_acq_active = True
            
            def continuous_acquisition_thread():
                import time
                acquisition_number = 1
                while self.continuous_acq_active:
                    # Update acquisition number in the status
                    def update_status(num):
                        self.acq_status_var.set(f"Acq #{num}...")
                    self.root.after(0, update_status, acquisition_number)
                    
                    # Log start of acquisition
                    if acquisition_number == 1:
                        self.root.after(0, lambda: self.log_message(f"Starting continuous acquisition..."))
                    
                    # Check flag before proceeding
                    if not self.continuous_acq_active:
                        self.root.after(0, lambda: self.log_message("Stopping before acquisition"))
                        break
                    
                    # Perform acquisition (this blocks until complete)
                    success = self._perform_single_acquisition(
                        study_name, run_name, operator, num_traces, 
                        trigger_rate, grating, scope_address, data_dir, 
                        autofit, acquisition_number
                    )
                    
                    # After acquisition completes, check if we should continue
                    if not self.continuous_acq_active:
                        self.root.after(0, lambda: self.log_message("Stopping after completing acquisition"))
                        break
                    
                    # If acquisition failed, break out
                    if not success:
                        self.root.after(0, lambda: self.log_message(f"Acquisition #{acquisition_number} FAILED - stopping continuous mode", logging.ERROR))
                        break
                    
                    acquisition_number += 1
                    
                    # Small delay to prevent overwhelming the scope
                    time.sleep(0.5)
                
                # Mark as finished and re-enable UI
                self.root.after(0, self._continuous_acquisition_complete)
            
            thread = threading.Thread(target=continuous_acquisition_thread)
            thread.daemon = True
            thread.start()
            
        else:
            # Single acquisition
            self.set_ui_state('acquiring')
            #self.acquire_button.config(state='disabled')
            self.acq_status_var.set("Acquiring...")
            
            self.log_message(f"Acquisition ({self.current_scope_type}): {study_name}/{run_name}, {num_traces} traces, {grating}µm grating")
            
            def acquisition_thread():
                success = self._perform_single_acquisition(
                    study_name, run_name, operator, num_traces, 
                    trigger_rate, grating, scope_address, data_dir, 
                    autofit, 1
                )
                self.root.after(0, self._acquisition_complete, success, 
                            "Acquisition complete" if success else "Acquisition failed")
            
            thread = threading.Thread(target=acquisition_thread)
            thread.daemon = True
            thread.start()

    def acquire_for_calibration(self):
        """Acquire data and use it for calibration (or update existing calibration)"""
        # Check if continuous acquisition is running
        if hasattr(self, 'continuous_acq_active') and self.continuous_acq_active:
            self.log_message("Cannot calibrate while continuous acquisition is running. Press Stop first.", logging.WARNING)
            return
        
        # Validate inputs
        study_name = self.study_name_var.get().strip()
        if not study_name:
            self.log_message("Please enter a Study Name", logging.WARNING)
            return
        
        run_name = self.run_name_var.get().strip()
        if not run_name:
            self.log_message("Please enter a run name", logging.WARNING)
            return
        
        operator = self.operator_var.get().strip()
        if not operator:
            self.log_message("Please enter an operator name", logging.WARNING)
            return
        
        try:
            num_traces = int(self.num_traces_var.get())
            if num_traces < 1:
                raise ValueError
        except ValueError:
            self.log_message("Number of traces must be a positive integer", logging.WARNING)
            return
        
        try:
            trigger_rate = float(self.trigger_rate_var.get())
        except ValueError:
            self.log_message("Trigger rate must be a number", logging.WARNING)
            return
        
        try:
            grating = float(self.acq_grating_var.get())
        except ValueError:
            self.log_message("Grating spacing must be a number", logging.WARNING)
            return
        
        scope_address = self.scope_address_var.get().strip()
        data_dir = self.data_dir_var.get().strip()
        
        if not data_dir:
            data_dir = r"C:\Users\short\Documents\Data"
        
        # Check if we're updating calibration (grating spacing already has a valid value)
        current_grating = self.grating_edit.get().strip()
        is_update = current_grating and float(current_grating) > 0
        
        if is_update:
            self.log_message(f"Updating calibration with new acquisition...")
        else:
            self.log_message(f"Calibration acquisition ({self.current_scope_type}): {study_name}/{run_name}, {num_traces} traces, {grating}µm grating")
        
        autofit = False  # Don't auto-fit, we'll calibrate instead
        
        # Single acquisition for calibration
        self.acq_status_var.set("Acquiring for calibration...")
        self.log_message(f"Calibration acquisition ({self.current_scope_type}): {study_name}/{run_name}, {num_traces} traces, {grating}µm grating")

        def calibration_acquisition_thread():
            success = self._perform_single_acquisition(
                study_name, run_name, operator, num_traces, 
                trigger_rate, grating, scope_address, data_dir, 
                autofit, 1  # acquisition_number = 1
            )
            if success:
                # Get the saved file paths
                today_date_str = datetime.now().strftime('%Y-%m-%d')
                base_folder = Path(data_dir) / study_name / today_date_str
                base_filename = f"{study_name}_{today_date_str}_{grating:.1f}_{run_name}"
                pos_file = str(base_folder / f"{base_filename}-POS-1.txt")
                neg_file = str(base_folder / f"{base_filename}-NEG-1.txt")
                
                # Run calibration on these files
                self.root.after(0, lambda: self._run_calibration_on_files(pos_file, neg_file))
            
            self.root.after(0, self._calibration_acquisition_complete, success)
        
        thread = threading.Thread(target=calibration_acquisition_thread)
        thread.daemon = True
        thread.start()

    def acquire_for_baseline(self):
        """Acquire data and use it as baseline reference (no analysis)"""
        # Check if continuous acquisition is running
        if hasattr(self, 'continuous_acq_active') and self.continuous_acq_active:
            self.log_message("Cannot acquire baseline while continuous acquisition is running. Press Stop first.", logging.WARNING)
            return
        
        # Validate inputs
        study_name = self.study_name_var.get().strip()
        if not study_name:
            self.log_message("Please enter a Study Name", logging.WARNING)
            return
        
        run_name = self.run_name_var.get().strip()
        if not run_name:
            self.log_message("Please enter a Run Name", logging.WARNING)
            return
        
        # Append '-baseline' to the run name for baseline files
        baseline_run_name = f"{run_name}-baseline"
        
        operator = self.operator_var.get().strip()
        if not operator:
            self.log_message("Please enter an Operator name", logging.WARNING)
            return
        
        try:
            num_traces = int(self.num_traces_var.get())
            if num_traces < 1:
                raise ValueError
        except ValueError:
            self.log_message("Number of traces must be a positive integer", logging.WARNING)
            return
        
        try:
            trigger_rate = float(self.trigger_rate_var.get())
        except ValueError:
            self.log_message("Trigger rate must be a number", logging.WARNING)
            return
        
        try:
            grating = float(self.acq_grating_var.get())
        except ValueError:
            self.log_message("Grating spacing must be a number", logging.WARNING)
            return
        
        scope_address = self.scope_address_var.get().strip()
        data_dir = self.data_dir_var.get().strip()
        
        if not data_dir:
            data_dir = r"C:\Users\short\Documents\Data"
        
        self.log_message(f"Baseline acquisition ({self.current_scope_type}): {study_name}/{baseline_run_name}, {num_traces} traces, {grating}µm grating")
        
        autofit = False  # Don't auto-fit baseline
        
        # Single acquisition for baseline
        self.acq_status_var.set("Acquiring baseline...")
        
        def baseline_acquisition_thread():
            success = self._perform_single_acquisition(
                study_name, baseline_run_name, operator, num_traces, 
                trigger_rate, grating, scope_address, data_dir, 
                autofit, 1
            )
            if success:
                # Get the saved file paths (using baseline_run_name)
                today_date_str = datetime.now().strftime('%Y-%m-%d')
                base_folder = Path(data_dir) / study_name / today_date_str
                base_filename = f"{study_name}_{today_date_str}_{grating:.1f}_{baseline_run_name}"
                pos_file = str(base_folder / f"{base_filename}-POS-1.txt")
                neg_file = str(base_folder / f"{base_filename}-NEG-1.txt")
                
                # Log the paths
                self.log_message(f"[DIAG] Baseline POS file: {pos_file}")
                self.log_message(f"[DIAG] Baseline NEG file: {neg_file}")
                self.log_message(f"[DIAG] File exists: {Path(pos_file).exists()}, {Path(neg_file).exists()}")
                
                # Read first few points to verify
                try:
                    from src.core.utils import read_data
                    test_pos = read_data(pos_file)
                    test_neg = read_data(neg_file)
                    self.log_message(f"[DIAG] Baseline POS first 5 values: {test_pos[:5, 1]}")
                    self.log_message(f"[DIAG] Baseline NEG first 5 values: {test_neg[:5, 1]}")
                    self.log_message(f"[DIAG] Baseline POS mean: {np.mean(test_pos[:,1]):.6f}")
                    self.log_message(f"[DIAG] Baseline NEG mean: {np.mean(test_neg[:,1]):.6f}")
                except Exception as e:
                    self.log_message(f"[DIAG] Error reading baseline files: {e}", logging.ERROR)
                
                # Set as baseline files on main thread
                self.root.after(0, lambda: self._set_baseline_files(pos_file, neg_file))
            else:
                self.log_message("Baseline acquisition failed!", logging.ERROR)
            
            self.root.after(0, self._baseline_acquisition_complete, success)
        
        thread = threading.Thread(target=baseline_acquisition_thread)
        thread.daemon = True
        thread.start()

    def _set_baseline_files(self, pos_file, neg_file):
        """Set baseline files and enable baseline correction"""
        self.log_message(f"\n[DIAG] Setting baseline files:")
        self.log_message(f"[DIAG] POS: {pos_file}")
        self.log_message(f"[DIAG] NEG: {neg_file}")
        
        # Verify files exist and have data
        if not Path(pos_file).exists() or not Path(neg_file).exists():
            self.log_message(f"[ERROR] Baseline files do not exist!", logging.ERROR)
            return
        
        try:
            from src.core.utils import read_data
            test_pos = read_data(pos_file)
            test_neg = read_data(neg_file)
            self.log_message(f"[DIAG] Baseline POS length: {len(test_pos)}, mean: {np.mean(test_pos[:,1]):.6f}")
            self.log_message(f"[DIAG] Baseline NEG length: {len(test_neg)}, mean: {np.mean(test_neg[:,1]):.6f}")
        except Exception as e:
            self.log_message(f"[ERROR] Cannot read baseline files: {e}", logging.ERROR)
            return
        
        self.baseline_pos_file = pos_file
        self.baseline_neg_file = neg_file
        
        # Update the label
        pos_name = Path(pos_file).stem
        neg_name = Path(neg_file).stem
        self.baseline_file_label.config(text=f"P: {pos_name} | N: {neg_name}", foreground=self.fg_color)
        
        # Enable baseline correction checkbox
        self.baseline_var.set(True)
        self.log_message(f"[DIAG] baseline_var set to: {self.baseline_var.get()}")
        
        # Update config
        self.config['signal_process']['baseline_correction']['enabled'] = True
        self.config['signal_process']['baseline_correction']['pos'] = pos_file
        self.config['signal_process']['baseline_correction']['neg'] = neg_file
        
        self.log_message(f"[DIAG] Config baseline enabled: {self.config['signal_process']['baseline_correction']['enabled']}")
        self.log_message(f"[DIAG] Config baseline POS: {self.config['signal_process']['baseline_correction']['pos']}")
        
        self.toggle_baseline_ui()
        
        self.log_message(f"Baseline set: {pos_name} and {neg_name}")
        self.log_message("Baseline correction enabled")

    def _baseline_acquisition_complete(self, success):
        """Handle baseline acquisition completion"""
        if success:
            self.acq_status_var.set("Ready - Baseline acquired and enabled")
        else:
            self.acq_status_var.set("Ready - Baseline acquisition failed")

    def _run_calibration_on_files(self, pos_file, neg_file):
        """Run calibration on acquired files and update grating spacing"""
        try:
            self.log_message(f"Running calibration on: {Path(pos_file).stem}")
            
            # Temporarily set calibration files
            original_calib_pos = self.calib_pos_file
            original_calib_neg = self.calib_neg_file
            original_sound_speed = self.sound_speed
            
            self.calib_pos_file = pos_file
            self.calib_neg_file = neg_file
            self.sound_speed = 2665.9  # m/s for tungsten (adjust as needed)
            
            # Run calibration (reuse existing calibration logic)
            temp_config = {
                'signal_process': {
                    'heterodyne': 'di-homodyne',
                    'null_point': int(self.start_point_var.get()),
                    'initial_samples': 50,
                    'baseline_correction': {'enabled': False}
                },
                'fft': self.config['fft'],
                'lorentzian': self.config['lorentzian'].copy(),
                'plot': {'signal_process': False, 'fft_lorentzian': False, 'tgs': False}
            }
            temp_config['lorentzian']['bimodal_fit'] = self.two_saw_var.get()
            
            signal, _, _, _ = process_signal(
                temp_config, None, 0, self.calib_pos_file, self.calib_neg_file, 
                grating_spacing=1.0, **temp_config['signal_process']
            )
            
            time = signal[:, 0]
            amp = signal[:, 1]
            dt = time[1] - time[0]
            derivative = np.gradient(amp, dt)
            saw_signal = np.column_stack((time[:-1], derivative[:-1]))
            
            fft_signal = fft(saw_signal, **temp_config['fft'])
            
            result = lorentzian_fit(
                temp_config, None, 0, fft_signal, **temp_config['lorentzian']
            )
            
            f = result[0]
            if isinstance(f, np.ndarray):
                f = f[0]
            
            grating_spacing_um = (self.sound_speed / f) * 1e6
            
            # Update the GUI with the calibrated value
            self.root.after(0, lambda: self._update_calibration_from_acquisition(grating_spacing_um, f))
            
            # Restore original calibration files (optional - could keep them)
            self.calib_pos_file = original_calib_pos
            self.calib_neg_file = original_calib_neg
            
        except Exception as e:
            self.log_message(f"Calibration failed: {str(e)}", logging.ERROR)
            self.root.after(0, lambda: messagebox.showerror("Calibration Error", f"Failed to calibrate:\n{str(e)}"))

    def _calibration_acquisition_complete(self, success):
        """Handle calibration acquisition completion"""
        # Don't disable any buttons
        if success:
            self.acq_status_var.set("Ready - Calibration complete")
            # Change button text to "Update calibration"
            self.acquire_calib_button.config(text="Update calibration")
        else:
            self.acq_status_var.set("Ready - Calibration failed")

    def _diagnose_scope_settings(self, scope):
        """Diagnostic: print all relevant scope settings"""
        try:
            if self.current_scope_type == self.SCOPE_RIGOL:
                timebase = scope.query(":TIMebase:MAIN:SCALe?")
                timebase_offset = scope.query(":TIMebase:MAIN:OFFSet?")
                sample_rate = scope.query(":ACQuire:SRATe?")
                mem_depth = scope.query(":ACQuire:MDEPth?")
                waveform_points = scope.query(":WAVeform:POINts?")
            else:  # LeCroy
                timebase = scope.query("TDIV?")
                timebase_offset = scope.query("TRDL?")
                sample_rate = scope.query("SAMPLE_RATE?")
                mem_depth = scope.query("MEM_DEPTH?")
                waveform_points = scope.query("WAVEFORM_SETUP? NUM_POINTS")
            
            self.log_message(f"  DIAG: Timebase={timebase.strip()} s/div")
            self.log_message(f"  DIAG: Offset={timebase_offset.strip()} s")
            self.log_message(f"  DIAG: Sample Rate={sample_rate.strip()} Sa/s")
            self.log_message(f"  DIAG: Memory Depth={mem_depth.strip()}")
            self.log_message(f"  DIAG: Waveform Points={waveform_points.strip()}")
        except Exception as e:
            self.log_message(f"  DIAG error: {e}")

    def _perform_single_acquisition(self, study_name, run_name, operator, num_traces, 
                                trigger_rate, grating, scope_address, data_dir, 
                                autofit, acquisition_number):
        """Perform a single acquisition and optionally fit the data"""
        try:
            import pyvisa
            import time
            from datetime import datetime
            
            rm = pyvisa.ResourceManager()
            scope = rm.open_resource(scope_address)
            
            # Longer timeout for LeCroy
            if self.current_scope_type == self.SCOPE_LECROY:
                scope.timeout = 60000  # 60 seconds
            else:
                scope.timeout = 30000  # 30 seconds
                
            # Important: Set no termination character for binary reads
            # We'll handle termination manually for LeCroy
            if self.current_scope_type == self.SCOPE_LECROY:
                scope.read_termination = None
                scope.write_termination = '\n'
            else:
                scope.read_termination = '\n'
                scope.write_termination = '\n'
            
            if self.current_scope_type == self.SCOPE_RIGOL:
                return self._perform_rigol_acquisition(
                    scope, rm, study_name, run_name, operator, num_traces,
                    trigger_rate, grating, data_dir, autofit, acquisition_number
                )
            else:
                return self._perform_lecroy_acquisition(scope, rm, study_name, run_name, operator, 
                                           num_traces, trigger_rate, grating, data_dir, 
                                           autofit, acquisition_number)
                    
        except Exception as e:
            self.log_message(f"Acquisition failed: {str(e)}", logging.ERROR)
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)
            return False

    def _perform_rigol_acquisition(self, scope, rm, study_name, run_name, operator, num_traces,
                                   trigger_rate, grating, data_dir, autofit, acquisition_number):
        """Perform acquisition from Rigol oscilloscope"""
        import time
        from datetime import datetime
        
        try:
            # Save original settings
            orig_scale = float(scope.query(":TIMebase:MAIN:SCALe?"))
            orig_offset = float(scope.query(":TIMebase:MAIN:OFFSet?"))

            self.orig_scale = orig_scale
            self.orig_offset = orig_offset
            self.scope_connection_string = scope.resource_name
            
            # Clear and configure averaging
            scope.write("*CLS")
            scope.write(":ACQuire:TYPE AVER")
            scope.write(f":ACQuire:AVERages {num_traces}")
            
            # Wait for averaging - check stop flag every 0.1 seconds
            wait_time = (num_traces / (trigger_rate * 1e3)) * 1.25
            
            # Simple loop with short sleeps - check flag frequently
            start_time = time.time()
            while (time.time() - start_time) < wait_time:
                if hasattr(self, 'continuous_acq_active') and not self.continuous_acq_active:
                    self.log_message(f"Acquisition #{acquisition_number} stopped by user")
                    scope.write(":RUN")
                    scope.close()
                    rm.close()
                    return False
                time.sleep(0.1)
            
            # Check again before stopping
            if hasattr(self, 'continuous_acq_active') and not self.continuous_acq_active:
                self.log_message(f"Acquisition #{acquisition_number} stopped after averaging")
                scope.write(":RUN")
                scope.close()
                rm.close()
                return False
            
            # Stop acquisition
            scope.write(":STOP")
            time.sleep(0.05)
            
            # Configure waveform settings
            scope.write(":WAVeform:POINts MAX")
            scope.write(":WAVeform:MODE NORM")
            scope.write(":WAVeform:FORMat WORD")
            
            # Get preamble from Channel 2
            scope.write(":WAVeform:SOURce CHANnel2")
            time.sleep(0.05)
            preamble = scope.query(":WAVeform:PREamble?")
            p = [float(x) for x in preamble.split(',')]
            
            nPts = int(p[2])
            dt = p[4]
            xorigin = p[5]
            xreference = p[6]
            
            # Acquire Channel 2 (POS)
            scope.write(":WAVeform:SOURce CHANnel2")
            time.sleep(0.05)
            data2 = scope.query_binary_values(":WAVeform:DATA?", datatype='H', is_big_endian=False)
            Y_pos_raw = np.array(data2, dtype=np.float64)
            
            y_increment = p[7]
            y_origin = p[8]
            Y_pos = (Y_pos_raw - y_origin) * y_increment
            
            # Acquire Channel 3 (NEG)
            scope.write(":WAVeform:SOURce CHANnel3")
            time.sleep(0.05)
            data3 = scope.query_binary_values(":WAVeform:DATA?", datatype='H', is_big_endian=False)
            Y_neg_raw = np.array(data3, dtype=np.float64)
            Y_neg = (Y_neg_raw - y_origin) * y_increment
            
            # Calculate time arrays
            i_pos = np.arange(len(Y_pos))
            i_neg = np.arange(len(Y_neg))
            
            T_pos = (i_pos - xreference) * dt + xorigin
            T_neg = (i_neg - xreference) * dt + xorigin
            
            # Shift to t=0
            T_pos = T_pos - T_pos[0]
            T_neg = T_neg - T_neg[0]
            
            # Restore scope settings
            scope.write(f":ACQuire:AVERages 512")
            scope.write(f":TIMebase:MAIN:SCALe {orig_scale:.10g}")
            scope.write(f":TIMebase:MAIN:OFFSet {orig_offset:.10g}")
            scope.write(":RUN")
            
            scope.close()
            rm.close()
            
            # Create timestamp and folder
            today_date_str = datetime.now().strftime('%Y-%m-%d')
            timestamp_str = datetime.now().strftime('%I:%M:%S %p')
            
            base_folder = Path(data_dir) / study_name / today_date_str
            base_folder.mkdir(parents=True, exist_ok=True)
            
            base_filename = f"{study_name}_{today_date_str}_{grating:.1f}_{run_name}"
            filename_neg = base_folder / f"{base_filename}-NEG-{acquisition_number}.txt"
            filename_pos = base_folder / f"{base_filename}-POS-{acquisition_number}.txt"
            
            self._write_scope_txt_file(filename_pos, 'POS', T_pos, Y_pos, study_name, today_date_str, 
                                    run_name, operator, grating, 2, nPts, 1, acquisition_number, dt, timestamp_str)
            self._write_scope_txt_file(filename_neg, 'NEG', T_neg, Y_neg, study_name, today_date_str,
                                    run_name, operator, grating, 3, nPts, 1, acquisition_number, dt, timestamp_str)
            
            self.log_message(f"Saved: {filename_pos.name}, {filename_neg.name}")
            
            # Auto-fit if requested
            if autofit:
                self._auto_fit_acquisition(str(filename_pos), str(filename_neg), study_name, run_name, acquisition_number)
                self.root.after(0, self.update_summary_plot)

            return True
            
        except Exception as e:
            self.log_message(f"Rigol acquisition failed: {str(e)}", logging.ERROR)
            try:
                scope.close()
            except:
                pass
            try:
                rm.close()
            except:
                pass
            return False

    def _read_lecroy_binary_response(self, scope, debug_name="unknown"):
        """
        Read a binary response from LeCroy scope.
        Format: #<digit_count><length_bytes><data>
        Returns the raw binary data (without the header).
        Also logs the raw data for debugging.
        """
        try:
            # Read header - first byte should be '#'
            header_byte = scope.read_bytes(1)
            if not header_byte:
                self.log_message(f"[LeCroy] No header byte received for {debug_name}")
                return None
            
            #self.log_message(f"[LeCroy] {debug_name} header byte: {header_byte.hex()} (ASCII: {header_byte if header_byte[0] < 128 else 'non-ASCII'})")
            
            if header_byte[0] != ord('#'):
                self.log_message(f"[LeCroy] Expected '#', got 0x{header_byte[0]:02x} for {debug_name}")
                return None
            
            # Read number of digits in length field
            num_digits_byte = scope.read_bytes(1)
            if not num_digits_byte:
                self.log_message(f"[LeCroy] No digit count byte for {debug_name}")
                return None
            
            num_digits = int(num_digits_byte.decode('ascii'))
            self.log_message(f"[LeCroy] {debug_name} digit count: {num_digits}")
            
            # Read the length string
            length_str = scope.read_bytes(num_digits).decode('ascii')
            data_length = int(length_str)
            self.log_message(f"[LeCroy] {debug_name} data length: {data_length} bytes")
            
            # Read the actual data
            binary_data = scope.read_bytes(data_length)
            self.log_message(f"[LeCroy] {debug_name} actual data read: {len(binary_data)} bytes")
            
            # Log first 64 bytes of binary data for debugging
            hex_preview = binary_data[:min(64, len(binary_data))].hex()
            self.log_message(f"[LeCroy] {debug_name} binary data preview (hex): {hex_preview}")
            
            # Try to show ASCII preview for printable bytes
            ascii_preview = []
            for b in binary_data[:min(64, len(binary_data))]:
                if 32 <= b < 127:
                    ascii_preview.append(chr(b))
                else:
                    ascii_preview.append('.')
            self.log_message(f"[LeCroy] {debug_name} binary data preview (ASCII): {''.join(ascii_preview)}")
            
            # Save raw binary data to file for later analysis
            try:
                debug_dir = Path("debug_lecroy")
                debug_dir.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                filename = debug_dir / f"{timestamp}_{debug_name}.bin"
                with open(filename, 'wb') as f:
                    f.write(binary_data)
                self.log_message(f"[LeCroy] Saved raw binary to: {filename}")
            except Exception as e:
                self.log_message(f"[LeCroy] Could not save debug file: {e}")
            
            return binary_data
            
        except Exception as e:
            self.log_message(f"[LeCroy] Error reading binary response for {debug_name}: {e}")
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)
            return None

    def _parse_lecroy_binary_descriptor(self, binary_data, channel):
        """
        Parse LeCroy binary descriptor data.
        The descriptor contains time parameters in binary format.
        Returns (dt, xorigin, num_points)
        """
        self.log_message(f"[LeCroy] Parsing descriptor for channel {channel}, data length: {len(binary_data)} bytes")
        
        try:
            if len(binary_data) >= 20:
                # Try big-endian first
                dt_be = struct.unpack('>d', binary_data[0:8])[0]
                xorigin_be = struct.unpack('>d', binary_data[8:16])[0]
                num_points_be = struct.unpack('>i', binary_data[16:20])[0]
                
                self.log_message(f"[LeCroy] Big-endian parse: dt={dt_be:.6e}, xorigin={xorigin_be:.6e}, points={num_points_be}")
                
                # Try little-endian
                dt_le = struct.unpack('<d', binary_data[0:8])[0]
                xorigin_le = struct.unpack('<d', binary_data[8:16])[0]
                num_points_le = struct.unpack('<i', binary_data[16:20])[0]
                
                self.log_message(f"[LeCroy] Little-endian parse: dt={dt_le:.6e}, xorigin={xorigin_le:.6e}, points={num_points_le}")
                
                # Determine which makes more sense
                dt = None
                xorigin = None
                num_points = None
                
                if 1e-12 < dt_be < 1e-3:
                    dt = dt_be
                    xorigin = xorigin_be
                    num_points = num_points_be
                    self.log_message(f"[LeCroy] Using big-endian parse (valid dt range)")
                elif 1e-12 < dt_le < 1e-3:
                    dt = dt_le
                    xorigin = xorigin_le
                    num_points = num_points_le
                    self.log_message(f"[LeCroy] Using little-endian parse (valid dt range)")
                else:
                    dt = dt_be
                    xorigin = xorigin_be
                    num_points = num_points_be
                    self.log_message(f"[LeCroy] Using big-endian parse (fallback)")
                
                # Sanity check values
                if dt <= 0 or dt > 1e-3:
                    self.log_message(f"[LeCroy] Unusual dt value: {dt}, using fallback")
                    dt = 1e-9
                
                if abs(xorigin) > 1e-3:
                    self.log_message(f"[LeCroy] Unusual xorigin value: {xorigin}, using 0")
                    xorigin = 0.0
                
                if num_points <= 0 or num_points > 10_000_000:
                    self.log_message(f"[LeCroy] Unusual num_points: {num_points}, using 10000")
                    num_points = 10000
                
                return dt, xorigin, num_points
            else:
                self.log_message(f"[LeCroy] Descriptor too short: {len(binary_data)} bytes")
                return 1e-9, 0.0, 10000
                
        except Exception as e:
            self.log_message(f"[LeCroy] Error parsing descriptor: {e}")
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)
            return 1e-9, 0.0, 10000

    def _parse_lecroy_binary_waveform_data(self, binary_data, scope, channel):
        """
        Parse binary waveform data from LeCroy.
        Data is typically 16-bit signed integers (big-endian).
        """
        self.log_message(f"[LeCroy] Parsing waveform data for channel {channel}, data length: {len(binary_data)} bytes")
        
        try:
            # Try parsing as 16-bit signed integers (big-endian)
            raw_data_be = np.frombuffer(binary_data, dtype='>i2')
            self.log_message(f"[LeCroy] Big-endian parse: {len(raw_data_be)} samples")
            
            # Try little-endian for comparison
            raw_data_le = np.frombuffer(binary_data, dtype='<i2')
            self.log_message(f"[LeCroy] Little-endian parse: {len(raw_data_le)} samples")
            
            # Determine which looks more reasonable
            use_be = True
            if len(raw_data_be) > 0 and len(raw_data_le) > 0:
                be_max = np.abs(raw_data_be).max()
                le_max = np.abs(raw_data_le).max()
                
                self.log_message(f"[LeCroy] Big-endian max abs: {be_max}, Little-endian max abs: {le_max}")
                
                if le_max < 32768 and le_max > 0:
                    use_be = False
                    self.log_message(f"[LeCroy] Using little-endian parse (values in valid ADC range)")
                elif be_max < 32768 and be_max > 0:
                    self.log_message(f"[LeCroy] Using big-endian parse (values in valid ADC range)")
                else:
                    self.log_message(f"[LeCroy] Using big-endian parse (default)")
            
            raw_data = raw_data_be if use_be else raw_data_le
            
            if len(raw_data) == 0:
                self.log_message(f"[LeCroy] No valid waveform data after parsing")
                return None
            
            # Log raw data statistics
            self.log_message(f"[LeCroy] Raw ADC stats - min: {raw_data.min()}, max: {raw_data.max()}, mean: {raw_data.mean():.1f}")
            
            # Get vertical scaling factors
            try:
                vdiv = float(scope.query(f"C{channel}:VOLTS_DIV?").strip())
                voff = float(scope.query(f"C{channel}:VOLT_OFF?").strip())
                self.log_message(f"[LeCroy] Vertical scaling: vdiv={vdiv} V/div, voff={voff} V")
                
                # Convert raw ADC values to voltage
                voltage_data = (raw_data.astype(np.float64) / 32768.0) * vdiv * 8 + voff
                
                self.log_message(f"[LeCroy] Converted voltage range: [{voltage_data.min():.3f}, {voltage_data.max():.3f}] V")
                
            except Exception as e:
                self.log_message(f"[LeCroy] Error getting vertical scaling: {e}, using raw values")
                voltage_data = raw_data.astype(np.float64)
                
                if np.abs(voltage_data).max() > 10:
                    self.log_message(f"[LeCroy] Large values detected ({voltage_data.max():.1f}), may need scaling")
            
            return voltage_data
            
        except Exception as e:
            self.log_message(f"[LeCroy] Error parsing binary waveform: {e}")
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)
            return None

    def _read_lecroy_binary_waveform_full(self, scope, channel):
        """
        Read waveform data from LeCroy channel using binary WF? commands.
        Handles both WF? DESC; (descriptor) and WF? DAT1; (waveform data).
        
        Returns: (voltage_data, dt, xorigin, num_points)
        """
        try:
            # --- Step 1: Get descriptor (timebase information) using WF? DESC; ---
            desc_cmd = f"C{channel}:WF? DESC;"
            self.log_message(f"[LeCroy] Sending: {desc_cmd}")
            
            # Write command, then read binary response (NOT query!)
            scope.write(desc_cmd)
            time.sleep(0.05)
            
            desc_binary = self._read_lecroy_binary_response(scope, f"DESC_ch{channel}")
            
            if desc_binary is None:
                self.log_message(f"[LeCroy] Failed to read descriptor for channel {channel}")
                dt = self._get_lecroy_dt(scope)
                xorigin = self._get_lecroy_xorigin(scope)
                num_points = 10000
            else:
                dt, xorigin, num_points = self._parse_lecroy_binary_descriptor(desc_binary, channel)
            
            self.log_message(f"[LeCroy] Channel {channel} descriptor: dt={dt*1e9:.2f}ns, xorigin={xorigin*1e9:.2f}ns, points={num_points}")
            
            # --- Step 2: Get waveform data using WF? DAT1; ---
            data_cmd = f"C{channel}:WF? DAT1;"
            self.log_message(f"[LeCroy] Sending: {data_cmd}")
            
            scope.write(data_cmd)
            time.sleep(0.05)
            
            wave_binary = self._read_lecroy_binary_response(scope, f"DAT1_ch{channel}")
            
            if wave_binary is None:
                self.log_message(f"[LeCroy] Failed to read waveform data for channel {channel}")
                return None, dt, xorigin, num_points
            
            voltage_data = self._parse_lecroy_binary_waveform_data(wave_binary, scope, channel)
            
            if voltage_data is None or len(voltage_data) == 0:
                self.log_message(f"[LeCroy] No valid waveform data for channel {channel}")
                return None, dt, xorigin, num_points
            
            if len(voltage_data) > num_points:
                voltage_data = voltage_data[:num_points]
            
            self.log_message(f"[LeCroy] Channel {channel}: {len(voltage_data)} points, range = [{voltage_data.min():.3f}, {voltage_data.max():.3f}] V")
            
            return voltage_data, dt, xorigin, num_points
            
        except Exception as e:
            self.log_message(f"[LeCroy] Error reading channel {channel}: {e}", logging.ERROR)
            return None, 0.0, 0.0, 0

    def _get_lecroy_dt(self, scope):
        """Get time interval (dt) from LeCroy scope using direct queries."""
        try:
            tdiv = float(scope.query("TDIV?").strip())
            try:
                wf_setup = scope.query("WAVEFORM_SETUP? NUM_POINTS").strip()
                num_points = int(float(wf_setup))
            except:
                num_points = 10000
            
            total_time = tdiv * 10
            dt = total_time / num_points
            return dt
            
        except Exception as e:
            self.log_message(f"[LeCroy] Error getting dt: {e}")
            return 1e-9

    def _get_lecroy_xorigin(self, scope):
        """Get horizontal offset (xorigin) from LeCroy scope."""
        try:
            trdl = float(scope.query("TRDL?").strip())
            return -trdl
        except Exception as e:
            self.log_message(f"[LeCroy] Error getting xorigin: {e}")
            return 0.0

    def _read_lecroy_vbs_waveform_data(self, scope, channel):
        """
        Fallback: Read waveform data from LeCroy channel using VBS to get DataArray.
        This is a text-based fallback if binary reading fails.
        """
        try:
            channel_str = f"C{channel}"
            
            data_vbs = f'''
            Dim dataArray, s, i, val
            dataArray = app.Acquisition.{channel_str}.Out.Result.DataArray
            s = ""
            For i = 0 To UBound(dataArray)
                val = dataArray(i)
                If IsNumeric(val) Then
                    If i > 0 Then s = s & ","
                    s = s & CStr(val)
                End If
            Next
            Return = s
            '''
            
            data_str = scope.query(data_vbs)
            
            if not data_str or len(data_str.strip()) == 0:
                self.log_message(f"[LeCroy] No data returned for channel {channel}")
                return None
            
            values = []
            for val_str in data_str.split(','):
                try:
                    values.append(float(val_str.strip()))
                except ValueError:
                    continue
            
            if len(values) == 0:
                self.log_message(f"[LeCroy] No valid numeric data for channel {channel}")
                return None
            
            voltage_data = np.array(values)
            
            dt = self._get_lecroy_dt(scope)
            xorigin = self._get_lecroy_xorigin(scope)
            
            if np.abs(voltage_data).max() > 10:
                try:
                    vdiv = float(scope.query(f"C{channel}:VOLTS_DIV?").strip())
                    voff = float(scope.query(f"C{channel}:VOLT_OFF?").strip())
                    voltage_data = (voltage_data / 32768.0) * vdiv * 8 + voff
                    self.log_message(f"[LeCroy] Applied scaling to channel {channel}")
                except:
                    pass
            
            self.log_message(f"[LeCroy] Channel {channel} (VBS fallback): {len(voltage_data)} points")
            
            return voltage_data
            
        except Exception as e:
            self.log_message(f"[LeCroy] VBS fallback error for channel {channel}: {e}", logging.ERROR)
            return None

    def _save_lecroy_original_settings(self, scope):
        """Save all original LeCroy scope settings before acquisition"""
        original_settings = {}
        try:
            # Save timebase settings
            original_settings['hor_scale'] = float(scope.query("VBS? 'return = app.Acquisition.Horizontal.HorScale'").strip() or 20e-9)
            original_settings['hor_offset'] = float(scope.query("VBS? 'return = app.Acquisition.Horizontal.HorOffset'").strip() or -80e-9)
            original_settings['sample_mode'] = scope.query("VBS? 'return = app.Acquisition.Horizontal.SampleMode'").strip()
            
            # Save NumSegments (if in Sequence mode)
            try:
                original_settings['num_segments'] = int(float(scope.query("VBS? 'return = app.Acquisition.Horizontal.NumSegments'").strip() or 1))
            except:
                original_settings['num_segments'] = 1
            
            # Save original channel settings - DO NOT SAVE View
            for ch in [2, 3]:
                original_settings[f'average_sweeps_{ch}'] = int(float(scope.query(f"VBS? 'return = app.Acquisition.C{ch}.AverageSweeps'").strip() or 1))
                original_settings[f'ver_scale_{ch}'] = float(scope.query(f"VBS? 'return = app.Acquisition.C{ch}.VerScale'").strip() or 0.1)
                original_settings[f'ver_offset_{ch}'] = float(scope.query(f"VBS? 'return = app.Acquisition.C{ch}.VerOffset'").strip() or 0.0)
            
            # Save original trigger mode
            original_settings['trigger_mode'] = scope.query("VBS? 'return = app.Acquisition.TriggerMode'").strip()
            
        except Exception as e:
            self.log_message(f"[LeCroy] Could not save original settings: {e}")
        return original_settings

    def _restore_lecroy_settings(self, scope, original_settings):
        """Restore LeCroy scope settings after acquisition"""
        try:
            
            # Restore timebase
            if 'hor_scale' in original_settings:
                scope.write(f"VBS 'app.Acquisition.Horizontal.HorScale = {original_settings['hor_scale']}'")
                time.sleep(0.05)
            if 'hor_offset' in original_settings:
                scope.write(f"VBS 'app.Acquisition.Horizontal.HorOffset = {original_settings['hor_offset']}'")
                time.sleep(0.05)
            
            # Restore SampleMode and NumSegments
            if 'sample_mode' in original_settings:
                scope.write(f"VBS 'app.Acquisition.Horizontal.SampleMode = \"{original_settings['sample_mode']}\"'")
                time.sleep(0.05)
            
            if 'num_segments' in original_settings and original_settings['sample_mode'] == "Sequence":
                scope.write(f"VBS 'app.Acquisition.Horizontal.NumSegments = {original_settings['num_segments']}'")
                time.sleep(0.05)
            
            # Restore channel settings - ONLY restore what we changed (AverageSweeps, VerScale, VerOffset)
            # DO NOT restore View - we never changed it during acquisition
            for ch in [2, 3]:
                # Restore average sweeps (this was changed during acquisition)
                if f'average_sweeps_{ch}' in original_settings:
                    scope.write(f"VBS 'app.Acquisition.C{ch}.AverageSweeps = {original_settings[f'average_sweeps_{ch}']}'")
                    time.sleep(0.05)
                
                # Restore vertical scale (this was possibly changed)
                if f'ver_scale_{ch}' in original_settings:
                    scope.write(f"VBS 'app.Acquisition.C{ch}.VerScale = {original_settings[f'ver_scale_{ch}']}'")
                    time.sleep(0.05)
                
                # Restore vertical offset (this was possibly changed)
                if f'ver_offset_{ch}' in original_settings:
                    scope.write(f"VBS 'app.Acquisition.C{ch}.VerOffset = {original_settings[f'ver_offset_{ch}']}'")
                    time.sleep(0.05)
            
            # Restore trigger mode (this was changed from Auto to Normal)
            if 'trigger_mode' in original_settings:
                scope.write(f"VBS 'app.Acquisition.TriggerMode = \"{original_settings['trigger_mode']}\"'")
                time.sleep(0.05)
            
        except Exception as e:
            self.log_message(f"[LeCroy] Could not restore settings: {e}")

    def _trim_lecroy_waveform(self, voltage_data, time_data, dt, xorigin):
        """
        Trim extra points from the beginning of LeCroy waveform.
        Find where the signal first deviates from baseline.
        Returns (trimmed_voltage_data, trimmed_time_data)
        """
        if voltage_data is None or len(voltage_data) < 100:
            return voltage_data, time_data
        
        # Calculate baseline from first 50 points (assuming they're pre-trigger)
        baseline = np.mean(voltage_data[:50])
        baseline_std = np.std(voltage_data[:50])
        
        # Find first point that exceeds 5 sigma above baseline
        threshold = baseline + 5 * baseline_std
        start_idx = 0
        for i in range(50, len(voltage_data)):
            if abs(voltage_data[i] - baseline) > threshold:
                start_idx = max(0, i - 10)  # Keep a few points before the trigger
                break
        
        if start_idx > 0:
            #self.log_message(f"[LeCroy] Trimming first {start_idx} points (baseline: {baseline:.4f}V, threshold: {threshold:.4f}V)")
            trimmed_voltage = voltage_data[start_idx:]
            trimmed_time = time_data[start_idx:]
            return trimmed_voltage, trimmed_time
        else:
            return voltage_data, time_data

    def _read_lecroy_waveform_with_header_strip(self, scope, channel):
        """
        Read waveform from LeCroy channel, properly stripping the binary header.
        Returns numpy array of voltage values.
        """
        try:
            # Read raw bytes
            scope.write(f"C{channel}:WAVEFORM?")
            raw_bytes = scope.read_raw()
            
            #self.log_message(f"[LeCroy] Channel {channel} raw bytes: {len(raw_bytes)} bytes")
            
            # Parse the binary header: #<digit_count><length_bytes>
            if len(raw_bytes) < 2:
                raise Exception("Response too short")
            
            # First byte should be '#'
            if raw_bytes[0] != ord('#'):
                # If not, maybe it's direct binary without header?
                self.log_message(f"[LeCroy] No '#' header, trying direct parse")
                data = np.frombuffer(raw_bytes, dtype='>i2')
                return data.astype(np.float64)
            
            # Get number of digits in length field
            num_digits = raw_bytes[1] - 48  # ASCII to int
            
            # Get data length from the next num_digits bytes
            length_str = raw_bytes[2:2+num_digits].decode('ascii')
            data_length = int(length_str)
            
            #self.log_message(f"[LeCroy] Channel {channel}: header says {data_length} bytes of data")
            
            # Extract just the waveform data (skip header)
            header_size = 2 + num_digits  # '#' + digit_count + length_bytes
            waveform_bytes = raw_bytes[header_size:header_size + data_length]
            
            #self.log_message(f"[LeCroy] Channel {channel}: extracted {len(waveform_bytes)} waveform bytes")
            
            # Parse as 16-bit big-endian integers
            data = np.frombuffer(waveform_bytes, dtype='>i2')
            #self.log_message(f"[LeCroy] Channel {channel}: parsed {len(data)} samples")
            
            return data.astype(np.float64)
            
        except Exception as e:
            self.log_message(f"[LeCroy] Error reading channel {channel}: {e}")
            return None

    def _perform_lecroy_acquisition(self, scope, rm, study_name, run_name, operator, num_traces,
                                trigger_rate, grating, data_dir, autofit, acquisition_number):
        """
        Perform acquisition from LeCroy oscilloscope using proper Automation commands.
        Based on LeCroy Automation Manual.
        """
        import time
        from datetime import datetime
        import numpy as np
        
        try:

            # --- 1. Clear and reset instrument ---
            scope.write("*CLS")
            time.sleep(0.1)
            
            # --- 2. Configure for binary data transfer ---
            scope.write("CHDR OFF")
            time.sleep(0.05)
            scope.write("CORD HI")
            time.sleep(0.05)
            scope.write("CFMT DEF9,WORD,BIN")
            time.sleep(0.1)
            
            # Save original settings BEFORE changing anything
            original_settings = {}
            try:
                # Save original timebase
                original_settings['hor_scale'] = float(scope.query("VBS? 'return = app.Acquisition.Horizontal.HorScale'").strip() or 20e-9)
                original_settings['hor_offset'] = float(scope.query("VBS? 'return = app.Acquisition.Horizontal.HorOffset'").strip() or -80e-9)
                original_settings['sample_mode'] = scope.query("VBS? 'return = app.Acquisition.Horizontal.SampleMode'").strip()
                
                # Save NumSegments (if in Sequence mode)
                try:
                    original_settings['num_segments'] = int(float(scope.query("VBS? 'return = app.Acquisition.Horizontal.NumSegments'").strip() or 1))
                except:
                    original_settings['num_segments'] = 1
                
                # Save original channel settings including averaging sweeps
                for ch in [2, 3]:
                    original_settings[f'view_{ch}'] = scope.query(f"VBS? 'return = app.Acquisition.C{ch}.View'").strip()
                    original_settings[f'average_sweeps_{ch}'] = int(float(scope.query(f"VBS? 'return = app.Acquisition.C{ch}.AverageSweeps'").strip() or 1))
                    original_settings[f'ver_scale_{ch}'] = float(scope.query(f"VBS? 'return = app.Acquisition.C{ch}.VerScale'").strip() or 0.1)
                    original_settings[f'ver_offset_{ch}'] = float(scope.query(f"VBS? 'return = app.Acquisition.C{ch}.VerOffset'").strip() or 0.0)
                
                # Save original trigger mode
                original_settings['trigger_mode'] = scope.query("VBS? 'return = app.Acquisition.TriggerMode'").strip()
                
                # self.log_message(f"[LeCroy] Saved original settings: hor_scale={original_settings['hor_scale']}, "
                #                 f"num_segments={original_settings.get('num_segments', 'N/A')}, "
                #                 f"C2_avg={original_settings['average_sweeps_2']}, "
                #                 f"trigger_mode={original_settings['trigger_mode']}")
            except Exception as e:
                self.log_message(f"[LeCroy] Could not save original settings: {e}")

            # --- 3. Configure channels using proper VBS Automation commands ---
            #self.log_message(f"[LeCroy] Setting up acquisition with {num_traces} traces...")
            
            # Configure C2 and C3 for averaging using VBS
            for ch in [2, 3]:
                # Set average sweeps count
                scope.write(f"VBS 'app.Acquisition.C{ch}.AverageSweeps = {num_traces}'")
                time.sleep(0.05)
                
                # Turn on the channel
                scope.write(f"VBS 'app.Acquisition.C{ch}.View = True'")
                time.sleep(0.05)
            
            # Configure Horizontal/Sequence mode (for faster acquisition)
            try:
                sample_mode = scope.query("VBS? 'return=app.Acquisition.Horizontal.SampleMode'").strip()
                #self.log_message(f"[LeCroy] Current sample mode: {sample_mode}")
                
                # Set to Sequence mode with 500 segments
                scope.write("VBS 'app.Acquisition.Horizontal.SampleMode = \"Sequence\"'")
                time.sleep(0.05)
                scope.write("VBS 'app.acquisition.horizontal.NumSegments = 1000'")
                time.sleep(0.1)
                #self.log_message("[LeCroy] Set Sequence mode with 1000 segments")
            except Exception as e:
                self.log_message(f"[LeCroy] Could not set Sequence mode: {e}")

            # --- 4. Set trigger mode to Normal using VBS ---
            scope.write("VBS 'app.Acquisition.TriggerMode = \"Normal\"'")
            time.sleep(0.05)
            
            # --- 5. Wait for acquisition to complete by polling sweeps via VBS ---
            target_sweeps = num_traces
            timeout_seconds = 300
            start_wait = time.time()
            
            #self.log_message(f"[LeCroy] Waiting for {target_sweeps} sweeps on C2 and C3...")
            
            last_c2_sweeps = 0
            last_c3_sweeps = 0
            
            while (time.time() - start_wait) < timeout_seconds:
                if hasattr(self, 'continuous_acq_active') and not self.continuous_acq_active:
                    self.log_message("[LeCroy] Acquisition stopped by user")
                    scope.write("*CLS")
                    return False
                
                try:
                    # Query sweeps using VBS
                    c2_sweeps_str = scope.query("VBS? 'return = app.Acquisition.C2.Out.Result.Sweeps'").strip()
                    c2_sweeps = int(float(c2_sweeps_str)) if c2_sweeps_str else 0
                    
                    c3_sweeps_str = scope.query("VBS? 'return = app.Acquisition.C3.Out.Result.Sweeps'").strip()
                    c3_sweeps = int(float(c3_sweeps_str)) if c3_sweeps_str else 0
                    
                    if c2_sweeps != last_c2_sweeps or c3_sweeps != last_c3_sweeps:
                        if c2_sweeps % 500 == 0 or c2_sweeps == target_sweeps:
                            self.log_message(f"[LeCroy] Sweeps: C2={c2_sweeps}/{target_sweeps}, C3={c3_sweeps}/{target_sweeps}")
                        last_c2_sweeps = c2_sweeps
                        last_c3_sweeps = c3_sweeps
                    
                    if c2_sweeps >= target_sweeps and c3_sweeps >= target_sweeps:
                        #self.log_message(f"[LeCroy] Acquisition completed after {time.time()-start_wait:.1f}s")
                        break
                        
                except Exception as e:
                    self.log_message(f"[LeCroy] Error polling sweeps: {e}")
                
                time.sleep(1)
            
            # --- 6. Read waveforms using WAVEFORM? (working method from debug) ---
            #self.log_message("[LeCroy] Reading waveforms...")
            
            # Get timebase info using VBS
            tdiv_str = scope.query("VBS? 'return = app.Acquisition.Horizontal.HorScale'").strip()
            tdiv = float(tdiv_str) if tdiv_str else 20e-9
            trdl_str = scope.query("VBS? 'return = app.Acquisition.Horizontal.HorOffset'").strip()
            trdl = float(trdl_str) if trdl_str else -80e-9

            # Read Channel 3 (POS signal)
            data_c3 = scope.query_binary_values("C3:WAVEFORM?", datatype='h', is_big_endian=True)
            if not data_c3 or len(data_c3) == 0:
                raise Exception("Failed to get waveform from Channel 3")
            Y_pos = np.array(data_c3, dtype=np.float64)

            # Read Channel 2 (NEG signal)
            data_c2 = scope.query_binary_values("C2:WAVEFORM?", datatype='h', is_big_endian=True)
            if not data_c2 or len(data_c2) == 0:
                raise Exception("Failed to get waveform from Channel 2")
            Y_neg = np.array(data_c2, dtype=np.float64)

            # Chop off the first 180 points which are garbage (if enough points exist)
            # Why did we do this? The headers of the waveforms the LeCroy scope sends over are undefeatable and must simply be chopped
            GARBAGE_POINTS = 180
            if len(Y_pos) > GARBAGE_POINTS:
                Y_pos = Y_pos[GARBAGE_POINTS:]
                Y_neg = Y_neg[GARBAGE_POINTS:]
                #self.log_message(f"[LeCroy] Chopped first {GARBAGE_POINTS} garbage points")

            # Get vertical scaling using VBS
            try:
                vdiv_c3_str = scope.query("VBS? 'return = app.Acquisition.C3.VerScale'").strip()
                vdiv_c3 = float(vdiv_c3_str) if vdiv_c3_str else 0.1
                voff_c3_str = scope.query("VBS? 'return = app.Acquisition.C3.VerOffset'").strip()
                voff_c3 = float(voff_c3_str) if voff_c3_str else 0.0
                
                vdiv_c2_str = scope.query("VBS? 'return = app.Acquisition.C2.VerScale'").strip()
                vdiv_c2 = float(vdiv_c2_str) if vdiv_c2_str else 0.1
                voff_c2_str = scope.query("VBS? 'return = app.Acquisition.C2.VerOffset'").strip()
                voff_c2 = float(voff_c2_str) if voff_c2_str else 0.0
                
                # Convert ADC counts to volts (16-bit ADC, ±8 divisions range)
                Y_pos = (Y_pos / 32768.0) * vdiv_c3 * 8 + voff_c3
                Y_neg = (Y_neg / 32768.0) * vdiv_c2 * 8 + voff_c2
                
            except Exception as e:
                self.log_message(f"[LeCroy] Error getting vertical scaling: {e}, using raw ADC values")

            # Calculate time array (use the original length before any trimming)
            num_points_raw = min(len(Y_pos), len(Y_neg))

            # dt = total_time / num_points, total_time = 10 divisions * tdiv
            dt = (tdiv * 10) / num_points_raw
            xorigin = -trdl  # Time offset

            # Create time array for raw data BEFORE trimming
            T_raw = np.arange(num_points_raw) * dt + xorigin
            T_raw = T_raw - T_raw[0]  # Shift to t=0

            # Now trim the waveforms and their corresponding time arrays together
            Y_pos, T_pos = self._trim_lecroy_waveform(Y_pos, T_raw, dt, xorigin)
            Y_neg, T_neg = self._trim_lecroy_waveform(Y_neg, T_raw, dt, xorigin)

            # Ensure both signals have the same length after trimming
            min_len = min(len(Y_pos), len(Y_neg))
            Y_pos = Y_pos[:min_len]
            Y_neg = Y_neg[:min_len]
            T_pos = T_pos[:min_len]
            T_neg = T_neg[:min_len]

            # self.log_message(f"[LeCroy] Final waveforms: {len(Y_pos)} points, dt = {dt*1e9:.2f} ns")
            # self.log_message(f"[LeCroy] C3 voltage range: [{Y_pos.min():.4f}, {Y_pos.max():.4f}] V")
            # self.log_message(f"[LeCroy] C2 voltage range: [{Y_neg.min():.4f}, {Y_neg.max():.4f}] V")
            
            # --- 7. Save files ---
            today_date_str = datetime.now().strftime('%Y-%m-%d')
            timestamp_str = datetime.now().strftime('%I:%M:%S %p')
            base_folder = Path(data_dir) / study_name / today_date_str
            base_folder.mkdir(parents=True, exist_ok=True)
            
            base_filename = f"{study_name}_{today_date_str}_{grating:.1f}_{run_name}"
            filename_pos = base_folder / f"{base_filename}-POS-{acquisition_number}.txt"
            filename_neg = base_folder / f"{base_filename}-NEG-{acquisition_number}.txt"
            
            self._write_scope_txt_file(filename_pos, 'POS', T_pos, Y_pos, study_name, today_date_str,
                                    run_name, operator, grating, 3, num_traces, 1, acquisition_number, dt, timestamp_str)
            self._write_scope_txt_file(filename_neg, 'NEG', T_neg, Y_neg, study_name, today_date_str,
                                    run_name, operator, grating, 2, num_traces, 1, acquisition_number, dt, timestamp_str)
            
            #self.log_message(f"[LeCroy] Saved: {filename_pos.name}, {filename_neg.name}")
            
            # --- 8. Auto-fit if requested ---
            if autofit:
                self._auto_fit_acquisition(str(filename_pos), str(filename_neg), study_name, run_name, acquisition_number)
                self.root.after(0, self.update_summary_plot)
            
            # --- 14. Restore original scope settings ---
            self._restore_lecroy_settings(scope, original_settings)

            return True
            
        except Exception as e:
            self.log_message(f"[LeCroy] Acquisition failed: {str(e)}", logging.ERROR)
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)
            return False

    def debug_lecroy_raw_waveform(self):
        """Debug: Read raw waveform response without parsing to see what the scope sends"""
        scope_address = self.scope_address_var.get().strip()
        if not scope_address:
            self.log_message("No scope address configured", logging.ERROR)
            return
        
        def debug_thread():
            try:
                import pyvisa
                import time
                from pathlib import Path
                from datetime import datetime
                
                self.log_message("=" * 60)
                self.log_message("DEBUG RAW: Reading raw response from LeCroy")
                self.log_message("=" * 60)
                
                rm = pyvisa.ResourceManager()
                scope = rm.open_resource(scope_address)
                scope.timeout = 10000
                
                # Configure
                scope.write("CHDR OFF")
                time.sleep(0.1)
                scope.write("CFMT DEF9,WORD,BIN")
                time.sleep(0.1)
                
                # Read the raw response as bytes without parsing
                scope.write("C2:WAVEFORM?")
                
                # Read raw bytes
                raw_bytes = scope.read_raw()
                self.log_message(f"Raw bytes length: {len(raw_bytes)}")
                self.log_message(f"First 100 bytes (hex): {raw_bytes[:100].hex()}")
                self.log_message(f"First 100 bytes (ascii): {raw_bytes[:100]}")
                
                # Save full raw response
                debug_dir = Path("debug_lecroy")
                debug_dir.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = debug_dir / f"{timestamp}_raw_response.bin"
                with open(filename, 'wb') as f:
                    f.write(raw_bytes)
                self.log_message(f"Saved raw response to: {filename}")
                
                scope.close()
                rm.close()
                
            except Exception as e:
                self.log_message(f"Debug failed: {e}", logging.ERROR)
                import traceback
                self.log_message(traceback.format_exc(), logging.DEBUG)
        
        thread = threading.Thread(target=debug_thread, daemon=True)
        thread.start()

    def _auto_fit_acquisition(self, pos_file, neg_file, study_name, run_name, acquisition_number):
        """Automatically fit the acquired data"""
        try:
            # Get current grating spacing from the calibration pane (NOT the acquisition pane)
            grating_text = self.grating_edit.get().strip()
            if not grating_text or float(grating_text) <= 0:
                # Fallback to config value if calibration pane is empty
                grating_text = str(self.config.get('tgs', {}).get('grating_spacing', 3.5276))
            
            # Update config with current settings
            null_point = self.start_point_var.get()
            if null_point < 1 or null_point > 4:
                null_point = 2
            
            self.config['signal_process']['null_point'] = int(null_point)
            self.config['lorentzian']['bimodal_fit'] = bool(self.two_saw_var.get())
            self.config['signal_process']['baseline_correction']['enabled'] = bool(self.baseline_var.get())
            self.config['tgs']['grating_spacing'] = float(grating_text)
            
            if self.baseline_var.get() and self.baseline_pos_file and self.baseline_neg_file:
                self.config['signal_process']['baseline_correction']['pos'] = self.baseline_pos_file
                self.config['signal_process']['baseline_correction']['neg'] = self.baseline_neg_file
            
            # Set plot saving to False for auto-fit
            original_close_plots = self.close_plots_var.get()
            self.close_plots_var.set(True)
            self.config['plot']['signal_process'] = False
            self.config['plot']['fft_lorentzian'] = False
            self.config['plot']['tgs'] = False
            
            # Create log file path
            data_dir = Path(pos_file).parent
            log_path = data_dir / f"{study_name}_{run_name}_postprocessing.txt"
            
            # Run the fit (stores data using base file_id)
            self._run_single_fit(log_path, pos_file, neg_file, acquisition_number)
            
            # Restore original settings
            self.close_plots_var.set(original_close_plots)
            
            file_id = Path(pos_file).stem
            
            # Remove old entry if it exists (check by base file_id)
            idx_to_remove = -1
            for i, f in enumerate(self.pos_files):
                f_path = Path(f)
                f_stem = f_path.stem
                if '-POS-' in f_stem:
                    f_base = f_stem.split('-POS-')[0]
                else:
                    f_base = f_stem
                if f_base == file_id:
                    idx_to_remove = i
                    break
            
            if idx_to_remove >= 0:
                self.pos_files.pop(idx_to_remove)
                self.neg_files.pop(idx_to_remove)
                # Remove from processed set if present
                if file_id in self.processed_files:
                    self.processed_files.remove(file_id)
            
            # Add to queue
            self.pos_files.append(pos_file)
            self.neg_files.append(neg_file)
            
            # Mark as processed if fit data exists (using base file_id)
            if file_id in self.file_to_fit_params:
                self.processed_files.add(file_id)
            
            # Update the listbox
            self.root.after(0, self._update_batch_listbox_processed_status)
            
            # Force summary plot update on main thread with a delay to ensure data is ready
            self.root.after(200, self.update_summary_plot)
            
            # Also update the fit preview if this is the most recent
            if file_id in self.file_to_fit_params:
                # Load plot data from disk
                fit_data = self.load_plot_data_from_disk(file_id, pos_file)
                if fit_data is not None:
                    self.root.after(200, lambda fd=fit_data: self.create_interactive_plot(fd))
            
            # If fit was successful, update the results table
            if file_id in self.file_to_fit_params:
                self.root.after(200, lambda fp=self.file_to_fit_params[file_id]: self.update_results_table(fp))
            
        except Exception as e:
            self.log_message(f"Auto-fit error: {str(e)}", logging.ERROR)
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)

    def _run_single_fit(self, log_path, pos_file, neg_file, acquisition_number):
        """Run a single fit for an acquired file - does NOT modify the batch queue"""
        from src.analysis.tgs import tgs_fit
        import matplotlib.pyplot as plt
        import copy
        
        plt.close('all')
        
        data_dir = Path(pos_file).parent
        
        from src.core.path import Paths
        paths = Paths(
            data_dir=data_dir,
            figure_dir=data_dir / 'figures',
            fit_dir=data_dir / 'fit',
            fit_path=data_dir / 'fit' / 'fit.csv',
            signal_path=data_dir / 'fit' / 'signal.json',
        )
        paths.figure_dir.mkdir(parents=True, exist_ok=True)
        paths.fit_dir.mkdir(parents=True, exist_ok=True)
        
        grating_spacing_val = float(self.config['tgs']['grating_spacing'])
        
        # Extract base file ID (without -POS-{number} suffix) for storage
        file_id = Path(pos_file).stem
        
        try:
            file_config = copy.deepcopy(self.config)
            
            file_config['signal_process']['null_point'] = int(file_config['signal_process']['null_point'])
            file_config['signal_process']['initial_samples'] = int(file_config['signal_process']['initial_samples'])
            file_config['lorentzian']['dc_filter_range'] = [
                int(file_config['lorentzian']['dc_filter_range'][0]),
                int(file_config['lorentzian']['dc_filter_range'][1])
            ]
            
            (start_idx, start_time, grating_spacing, 
            A, A_err, B, B_err, C, C_err, 
            alpha, alpha_err, beta, beta_err, 
            theta, theta_err, tau, tau_err, 
            f, f_err, signal, fft_full, lorentzian_curve) = tgs_fit(
                file_config, paths, acquisition_number, str(pos_file), str(neg_file), 
                grating_spacing=grating_spacing_val,
                signal_proportion=float(file_config['tgs']['signal_proportion']),
                maxfev=int(file_config['tgs']['maxfev'])
            )
            
            plt.close('all')
            
            date_time_str = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
            
            # Store fit parameters using base file_id
            fit_params = {
                'A': float(A) if A is not None else None,
                'A_err': float(A_err) if A_err is not None else None,
                'B': float(B) if B is not None else None,
                'B_err': float(B_err) if B_err is not None else None,
                'C': float(C) if C is not None else None,
                'C_err': float(C_err) if C_err is not None else None,
                'alpha': float(alpha) if alpha is not None else None,
                'alpha_err': float(alpha_err) if alpha_err is not None else None,
                'beta': float(beta) if beta is not None else None,
                'beta_err': float(beta_err) if beta_err is not None else None,
                'theta': float(theta) if theta is not None else None,
                'theta_err': float(theta_err) if theta_err is not None else None,
                'tau': float(tau) if tau is not None else None,
                'tau_err': float(tau_err) if tau_err is not None else None,
                'f': float(f) if f is not None else None,
                'f_err': float(f_err) if f_err is not None else None,
            }
            
            # Create plot data using base file_id
            fit_data_for_plot = {
                'title': file_id,
                'time_raw': signal[:, 0],
                'signal_raw': signal[:, 1],
            }
            
            time_fit = np.linspace(signal[start_idx, 0], signal[-1, 0], 1000)
            from src.analysis.functions import tgs_function
            functional_func, thermal_func = tgs_function(start_time, grating_spacing_val)
            
            fit_data_for_plot['time_fit'] = time_fit
            fit_data_for_plot['fit_signal'] = functional_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
            fit_data_for_plot['thermal_signal'] = thermal_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
            
            if fft_full is not None and len(fft_full) > 0:
                fit_data_for_plot['fft_freq'] = fft_full[:, 0]
                fit_data_for_plot['fft_amp'] = fft_full[:, 1]
            else:
                fit_data_for_plot['fft_freq'] = None
                fit_data_for_plot['fft_amp'] = None
            
            # Store only fit parameters (plot data will be regenerated on demand)
            self.file_to_fit_params[file_id] = fit_params
            
            # Log success
            self.log_message(f"  Auto-fit SUCCESS for {file_id}")
            
            # Append to results log
            self._append_to_results_log(log_path, file_id, date_time_str, grating_spacing, 
                                    f, f_err, A, A_err, alpha, alpha_err, beta, beta_err,
                                    B, B_err, theta, theta_err, tau, tau_err, C, C_err)
            
        except Exception as e:
            self.log_message(f"  Auto-fit FAILED for {file_id}: {str(e)}", logging.ERROR)
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)

    def load_plot_data_from_disk(self, file_id, pos_file):
        """Load plot data from disk for a given file_id - regenerates all fits from parameters"""
        try:
            data_dir = Path(pos_file).parent
            fit_csv_path = data_dir / 'fit' / 'fit.csv'
            
            fit_data = {'title': file_id}
            
            # Get the corresponding neg file
            neg_file = pos_file.replace('POS', 'NEG')
            if not Path(neg_file).exists():
                for i, p in enumerate(self.pos_files):
                    if Path(p).stem == Path(pos_file).stem:
                        if i < len(self.neg_files):
                            neg_file = self.neg_files[i]
                            break
            
            # Load signal data by reprocessing from raw files
            try:
                from src.analysis.signal_process import process_signal
                from src.core.path import Paths
                from src.analysis.functions import tgs_function, lorentzian_function
                from src.analysis.fft import fft
                
                # Create a temporary config for loading
                temp_config = self.config.copy()
                temp_config['plot']['signal_process'] = False
                
                paths = Paths(
                    data_dir=data_dir,
                    figure_dir=data_dir / 'figures',
                    fit_dir=data_dir / 'fit',
                    fit_path=data_dir / 'fit' / 'fit.csv',
                    signal_path=data_dir / 'fit' / 'signal.json',
                )
                
                import re
                pos_stem = Path(pos_file).stem
                match = re.search(r'POS-(\d+)$', pos_stem)
                acq_num = int(match.group(1)) if match else 0
                
                grating_spacing = float(self.grating_edit.get().strip()) if self.grating_edit.get().strip() else self.config.get('tgs', {}).get('grating_spacing', 3.5276)
                
                # Process the signal
                signal, max_time, start_time, start_idx = process_signal(
                    temp_config, 
                    paths, 
                    acq_num,
                    str(pos_file), 
                    str(neg_file),
                    grating_spacing,
                    **temp_config['signal_process']
                )
                
                fit_data['time_raw'] = signal[:, 0]
                fit_data['signal_raw'] = signal[:, 1]
                fit_data['start_idx'] = start_idx
                fit_data['start_time'] = start_time
                fit_data['max_time'] = max_time
                
                # Load fit parameters from CSV or from stored params
                A = B = C = alpha = beta = theta = tau = f = None
                
                # First try from CSV
                if fit_csv_path.exists():
                    import pandas as pd
                    df = pd.read_csv(fit_csv_path)
                    
                    # Find the row for this file
                    row = df[df['run_name'] == file_id]
                    if row.empty:
                        row = df[df['run_name'].str.contains(file_id)]
                    
                    if not row.empty:
                        A = float(row['A[Wm^-2]'].values[0]) if 'A[Wm^-2]' in row else None
                        B = float(row['B[Wm^-2]'].values[0]) if 'B[Wm^-2]' in row else None
                        C = float(row['C[Wm^-2]'].values[0]) if 'C[Wm^-2]' in row else None
                        alpha = float(row['alpha[m^2s^-1]'].values[0]) if 'alpha[m^2s^-1]' in row else None
                        beta = float(row['beta[s^0.5]'].values[0]) if 'beta[s^0.5]' in row else None
                        theta = float(row['theta[rad]'].values[0]) if 'theta[rad]' in row else None
                        tau = float(row['tau[s]'].values[0]) if 'tau[s]' in row else None
                        f = float(row['f[Hz]'].values[0]) if 'f[Hz]' in row else None
                        start_time = float(row['start_time'].values[0]) if 'start_time' in row else start_time
                        grating_spacing_um = float(row['grating_spacing[µm]'].values[0]) if 'grating_spacing[µm]' in row else grating_spacing
                else:
                    # Try from stored params
                    params = self.file_to_fit_params.get(file_id, {})
                    A = params.get('A')
                    B = params.get('B')
                    C = params.get('C')
                    alpha = params.get('alpha')
                    beta = params.get('beta')
                    theta = params.get('theta')
                    tau = params.get('tau')
                    f = params.get('f')
                
                # Generate fits if we have valid parameters
                if all(v is not None for v in [A, B, C, alpha, beta, theta, tau, f]):
                    # Build TGS fit functions
                    functional_func, thermal_func = tgs_function(start_time, grating_spacing)
                    
                    # Generate time points for fit
                    time_fit = np.linspace(
                        max(start_time, fit_data['time_raw'][0]), 
                        fit_data['time_raw'][-1], 
                        1000
                    )
                    
                    # Generate functional and thermal fits
                    fit_data['time_fit'] = time_fit
                    fit_data['fit_signal'] = functional_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
                    fit_data['thermal_signal'] = thermal_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
                    
                    # Generate FFT data from the signal
                    # Remove thermal background to get SAW signal
                    thermal_bg = thermal_func(fit_data['time_raw'], A, B, C, alpha, beta, theta, tau, f)
                    saw_signal = np.column_stack([
                        fit_data['time_raw'], 
                        fit_data['signal_raw'] - thermal_bg
                    ])
                    
                    fft_config = self.config.get('fft', {})
                    fft_signal = fft(
                        saw_signal,
                        signal_proportion=fft_config.get('signal_proportion', 1.0),
                        use_derivative=fft_config.get('use_derivative', True),
                        analysis_type=fft_config.get('analysis_type', 'psd')
                    )
                    
                    if fft_signal is not None and len(fft_signal) > 0:
                        fft_freq_ghz = fft_signal[:, 0] / 1e9
                        fit_data['fft_freq'] = fft_freq_ghz
                        fit_data['fft_amp'] = fft_signal[:, 1]
                        
                        # Generate Lorentzian fit
                        freq_bounds = self.config.get('lorentzian', {}).get('frequency_bounds', [0.1, 0.9])
                        lorentzian_freqs = np.linspace(freq_bounds[0], freq_bounds[1], 500)
                        
                        # Estimate width from tau
                        width = 1 / (2 * np.pi * tau * 1e9) if tau > 0 else 0.05
                        f_ghz = f / 1e9
                        
                        # Scale Lorentzian to match FFT amplitude
                        mask = (fft_freq_ghz >= freq_bounds[0]) & (fft_freq_ghz <= freq_bounds[1])
                        if np.any(mask):
                            peak_fft = np.max(fft_signal[mask, 1])
                            lorentzian_amp = peak_fft * width**2
                        else:
                            lorentzian_amp = 1.0
                        
                        fit_data['lorentzian_freq'] = lorentzian_freqs
                        fit_data['lorentzian_fit'] = lorentzian_function(
                            lorentzian_freqs, 
                            lorentzian_amp, 
                            f_ghz, 
                            width, 
                            0
                        )
                        
                        # Store the peak frequency for reference
                        fit_data['peak_freq_ghz'] = f_ghz
                        fit_data['peak_freq_hz'] = f
                        
                        # Also generate Lorentzian fit on the actual FFT range
                        fit_data['lorentzian_freq_full'] = fft_freq_ghz
                        fit_data['lorentzian_fit_full'] = lorentzian_function(
                            fft_freq_ghz,
                            lorentzian_amp,
                            f_ghz,
                            width,
                            0
                        )
                    
                    # Debug logging
                    self.log_message(f"[PLOT] Regenerated plot for {file_id}: A={A:.3e}, alpha={alpha:.3e}, f={f/1e6:.3f} MHz", logging.DEBUG)
                    
                else:
                    self.log_message(f"[PLOT] Missing parameters for {file_id}: A={A}, B={B}, alpha={alpha}, f={f}", logging.WARNING)
                    
            except Exception as e:
                self.log_message(f"Could not reprocess signal: {e}", logging.DEBUG)
                import traceback
                self.log_message(traceback.format_exc(), logging.DEBUG)
                
                # Fallback: try to load from JSON
                signal_path = data_dir / 'fit' / 'signal.json'
                if signal_path.exists():
                    try:
                        import json
                        with open(signal_path, 'r') as f:
                            signals = json.load(f)
                        
                        for signal_entry in signals:
                            if isinstance(signal_entry, dict) and signal_entry.get('file_id') == file_id:
                                signal_array = np.array(signal_entry['data'])
                                fit_data['time_raw'] = signal_array[:, 0]
                                fit_data['signal_raw'] = signal_array[:, 1]
                                break
                    except:
                        pass
            
            return fit_data
        except Exception as e:
            self.log_message(f"Could not load plot data from disk: {e}", logging.DEBUG)
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)
            return None

    def _append_to_results_log(self, log_path, run_name, date_time, grating_spacing_um,
                            f_val, f_err_val, A, A_err, alpha, alpha_err, 
                            beta, beta_err, B, B_err, theta, theta_err, 
                            tau_val, tau_err_val, C, C_err):
        """Append a single result to the results log file"""
        
        # Check if file exists to determine if we need to write header
        file_exists = Path(log_path).exists()
        
        headers = [
            'run_name', 'date_time', 'grating_spacing_um', 
            'SAW_freq_Hz', 'SAW_freq_error_Hz',
            'A_Wm-2', 'A_err_Wm-2', 'alpha_m2s-1', 'alpha_err_m2s-1',
            'beta_s0.5', 'beta_err_s0.5', 'B_Wm-2', 'B_err_Wm-2',
            'theta_rad', 'theta_err_rad', 'tau_s', 'tau_err_s',
            'C_Wm-2', 'C_err_Wm-2'
        ]
        
        with open(log_path, 'a') as f:
            if not file_exists:
                f.write(' '.join(headers) + '\n')
            
            row = [
                run_name, date_time,
                f"{grating_spacing_um:.8e}",
                f"{f_val:.8e}", f"{f_err_val:.8e}",
                f"{A:.8e}", f"{A_err:.8e}" if A_err is not None else "NaN",
                f"{alpha:.8e}", f"{alpha_err:.8e}" if alpha_err is not None else "NaN",
                f"{beta:.8e}", f"{beta_err:.8e}" if beta_err is not None else "NaN",
                f"{B:.8e}", f"{B_err:.8e}" if B_err is not None else "NaN",
                f"{theta:.8e}", f"{theta_err:.8e}" if theta_err is not None else "NaN",
                f"{tau_val:.8e}", f"{tau_err_val:.8e}" if tau_err_val is not None else "NaN",
                f"{C:.8e}", f"{C_err:.8e}" if C_err is not None else "NaN"
            ]
            
            f.write(' '.join(row) + '\n')
        
        self.log_message(f"Results appended to {log_path}")

    def _acquisition_complete(self, success, message):
        """Handle acquisition completion"""
        if success:
            self.acq_status_var.set("Ready")
        else:
            self.acq_status_var.set("Failed")
            self.log_message(message, logging.ERROR)
            messagebox.showerror("Error", f"Acquisition failed:\n{message}")

    def _write_scope_txt_file(self, filename, signal_name, time_data, amplitude,
                            study_name, date_str, run_name, operator, grating,
                            channel, num_traces, batch_files, batch_number, dt, timestamp_str):
        """Write scope data to TXT file"""
        try:
            #self.log_message(f"[DEBUG] Writing to {filename}")
            #self.log_message(f"[DEBUG] Time data length: {len(time_data)}, Amplitude length: {len(amplitude)}")
            
            with open(filename, 'w') as f:
                # Format grating with 'um' suffix
                try:
                    grating_val = float(grating)
                    grating_str = f"{grating_val}um"
                except:
                    grating_str = str(grating)
                    if 'um' not in grating_str.lower():
                        grating_str = f"{grating_str}um"
                
                f.write(f'Study Name\t{study_name}\n')
                f.write(f'Sample Name\t{date_str}\n')
                f.write(f'Run Name\t{run_name}\n')
                f.write(f'Operator\t{operator}\n')
                f.write(f'Date\t{date_str}\n')
                f.write(f'Time\t{timestamp_str}\n')
                f.write(f'Sign\t{signal_name}\n')
                f.write(f'Grating Spacing\t{grating_str}\n')
                f.write(f'Channel\t{channel}\n')
                f.write(f'Number Traces\t{num_traces}\n')
                f.write(f'Files in Batch\t{batch_files}\n')
                f.write(f'Batch Number\t{batch_number}\n')
                
                if dt >= 1:
                    f.write(f'dt\t{dt:.6f}\n')
                else:
                    f.write(f'dt\t{dt:.6E}\n')
                
                f.write(f'time stamp (ms)\t{timestamp_str}\n')
                f.write('\nTime\tAmplitude\n')
                
                # Write data points
                for i in range(len(time_data)):
                    f.write(f'{time_data[i]:.6E}\t{amplitude[i]:.6E}\n')
            
            #self.log_message(f"[DEBUG] Successfully wrote {len(time_data)} points to {filename}")
        except Exception as e:
            self.log_message(f"Error writing {filename}: {str(e)}", logging.ERROR)
            import traceback
            self.log_message(traceback.format_exc(), logging.DEBUG)

    def build_calibration_section(self, parent):
        """Section 1: Calibration (grating spacing)"""
        frame = ttk.LabelFrame(parent, text="Calibration (grating spacing)", padding=(15, 10))
        frame.pack(fill='x', pady=(0, 15))
        
        # File selection row
        file_row = ttk.Frame(frame, style='Panel.TFrame')
        file_row.pack(fill='x', pady=8)
        
        btn = self.create_button(file_row, text="Select calibration files", 
                    command=self.select_calib_files)
        btn.pack(side='left', padx=(0, 15))
        self.add_tooltip(btn, "Select POS and NEG files for calibration (must be a matching pair)")
        
        # Make label background transparent
        self.calib_file_label = ttk.Label(file_row, text="No files selected", foreground=self.fg_color)
        self.calib_file_label.pack(side='left', fill='x', expand=True)
        
        # Grating spacing row
        spacing_row = ttk.Frame(frame, style='Panel.TFrame')
        spacing_row.pack(fill='x', pady=8)
        
        lbl = ttk.Label(spacing_row, text="Grating (µm):", background='')
        lbl.pack(side='left', padx=(0, 15))
        self.add_tooltip(lbl, "Calculated grating spacing from calibration (or manually entered)")
        
        self.grating_edit = ttk.Entry(spacing_row, width=15)
        self.grating_edit.pack(side='left', padx=(0, 15))
        self.grating_edit.insert(0, "0")
        self.add_tooltip(self.grating_edit, "Grating spacing in micrometers (µm)")
        
        btn = self.create_button(spacing_row, text="Run calibration", 
                    command=self.run_calibration)
        btn.pack(side='left')
        self.add_tooltip(btn, "Run calibration using selected files and known sound speed in {100} tungsten (2665.9 m/s)")
        
        self.sound_speed = 2665.9
    
    def build_parameters_section(self, parent):
        """Section 2: Global fit parameters with modern styling"""
        frame = ttk.LabelFrame(parent, text="Global fit parameters", padding=(15, 10))
        frame.pack(fill='x', pady=(0, 15))
        
        # Start point and checkboxes row
        start_row = ttk.Frame(frame, style='Panel.TFrame')
        start_row.pack(fill='x', pady=8)
        
        lbl = ttk.Label(start_row, text="Start point (1-4):", background='')
        lbl.pack(side='left', padx=(0, 15))
        self.add_tooltip(lbl, "Null point selection for TGS signal phase analysis (valid range: 1-4)")
        
        self.start_point_var = tk.IntVar(value=self.config['signal_process']['null_point'])
        spin = ttk.Spinbox(start_row, from_=1, to=4, textvariable=self.start_point_var, width=5)
        spin.pack(side='left', padx=(0, 25))
        self.add_tooltip(spin, "Select which null point to use for fitting (1-4)")
        
        self.two_saw_var = tk.BooleanVar(value=self.config['lorentzian']['bimodal_fit'])
        cb = ttk.Checkbutton(start_row, text="Two SAW", variable=self.two_saw_var)
        cb.pack(side='left', padx=(0, 25))
        self.add_tooltip(cb, "Enable bimodal Lorentzian fitting for two SAW peaks")
        
        self.close_plots_var = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(start_row, text="Disable plot saving", variable=self.close_plots_var)
        cb.pack(side='left')
        self.add_tooltip(cb, "Check to disable saving plot images (uncheck to save plots)")
        
        # Baseline row
        baseline_row = ttk.Frame(frame, style='Panel.TFrame')
        baseline_row.pack(fill='x', pady=8)
        
        self.baseline_var = tk.BooleanVar(value=self.config['signal_process']['baseline_correction']['enabled'])
        cb = ttk.Checkbutton(baseline_row, text="Use baseline", variable=self.baseline_var,
                       command=self.toggle_baseline_ui)
        cb.pack(side='left', padx=(0, 15))
        self.add_tooltip(cb, "Enable baseline correction using reference files")
        
        self.baseline_button = self.create_button(baseline_row, text="Select baseline", 
                                         command=self.select_baseline_files, 
                                        state='normal')
        self.baseline_button.pack(side='left', padx=(0, 15))
        self.add_tooltip(self.baseline_button, "Select POS and NEG baseline reference files")
        
        self.baseline_file_label = ttk.Label(baseline_row, text="No baseline selected", foreground=self.fg_color, background='')
        self.baseline_file_label.pack(side='left', fill='x', expand=True)
        
        btn = self.create_button(frame, text="Edit all parameters...", 
                    command=self.open_config_editor)
        btn.pack(fill='x', pady=12)
        self.add_tooltip(btn, "Open full configuration editor with all fitting settings")
    
    def build_batch_section(self, parent):
        """Section 3: Batch processing queue"""
        frame = ttk.LabelFrame(parent, text="3. Batch processing queue", padding=(15, 10))
        frame.pack(fill='both', expand=True)
        
        # Button row - use Panel.TFrame style
        btn_row = ttk.Frame(frame, style='Panel.TFrame')
        btn_row.pack(fill='x', pady=(0, 12))

        # Make buttons smaller and more compact
        self.add_button = self.create_button(btn_row, text="Add files", command=self.add_batch_files)
        self.add_button.pack(side='left', padx=(0, 10))
        self.add_tooltip(self.add_button, "Add TGS files to batch processing queue (select both POS and NEG files)")

        self.clear_queue_button = self.create_button(btn_row, text="Clear queue", command=self.clear_queue)
        self.clear_queue_button.pack(side='left', padx=(0, 10))
        self.add_tooltip(self.clear_queue_button, "Clear all files from the batch queue")

        self.remove_button = self.create_button(btn_row, text="Remove", command=self.remove_selected_item)
        self.remove_button.pack(side='left', padx=(0, 10))
        self.add_tooltip(self.remove_button, "Remove selected run from queue and results log")

        # Create stop button for batch processing
        self.stop_button = self.create_button(btn_row, text="Stop", command=self.stop_batch_processing, state='normal')
        self.stop_button.pack(side='left')
        self.add_tooltip(self.stop_button, "Stop the current batch processing (finishes current file)")
        
        # Listbox with scrollbar
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill='both', expand=True, pady=(0, 12))
        
        scrollbar = ttk.Scrollbar(list_frame)
        
        self.batch_listbox = tk.Listbox(
            list_frame, 
            yscrollcommand=scrollbar.set,
            bg='#3c3c3c',
            fg='#f0f0f0',
            selectbackground='#404040',
            selectforeground='#f0f0f0',
            relief='flat',
            borderwidth=0,
            highlightthickness=0,
            font=('Consolas', 9)
        )
        self.batch_listbox.pack(side='left', fill='both', expand=True)
        
        def update_scrollbar_visibility(event=None):
            if self.batch_listbox.size() > 0:
                if self.batch_listbox.bbox(0) is not None:
                    last_index = self.batch_listbox.size() - 1
                    last_bbox = self.batch_listbox.bbox(last_index)
                    if last_bbox:
                        listbox_height = self.batch_listbox.winfo_height()
                        if last_bbox[1] + last_bbox[3] > listbox_height:
                            scrollbar.pack(side='right', fill='y')
                        else:
                            scrollbar.pack_forget()
                    else:
                        scrollbar.pack_forget()
                else:
                    scrollbar.pack_forget()
            else:
                scrollbar.pack_forget()
        
        self.batch_listbox.bind('<<ListboxSelect>>', update_scrollbar_visibility)
        self.batch_listbox.bind('<Configure>', update_scrollbar_visibility)
        
        def on_items_changed():
            self.root.after(100, update_scrollbar_visibility)
        
        original_insert = self.batch_listbox.insert
        original_delete = self.batch_listbox.delete
        
        def custom_insert(*args):
            original_insert(*args)
            on_items_changed()
        
        def custom_delete(*args):
            original_delete(*args)
            on_items_changed()
        
        self.batch_listbox.insert = custom_insert
        self.batch_listbox.delete = custom_delete
        
        scrollbar.config(command=self.batch_listbox.yview)
        self.batch_listbox.bind('<<ListboxSelect>>', self.on_batch_item_selected)
        
        # Export settings
        export_frame = ttk.LabelFrame(frame, text="Export Settings", padding=(12, 8))
        export_frame.pack(fill='x', pady=(0, 10))

        # Use Panel.TFrame style for the inner frame to match panel background
        log_row = ttk.Frame(export_frame, style='Panel.TFrame')
        log_row.pack(fill='x', pady=8)

        lbl = ttk.Label(log_row, text="Output Folder:")
        lbl.pack(side='left', padx=(0, 15))
        self.add_tooltip(lbl, "Path where the results log file will be saved")

        self.log_file_var = tk.StringVar()
        entry = ttk.Entry(log_row, textvariable=self.log_file_var, style='Panel.TEntry')
        entry.pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.add_tooltip(entry, "File path for saving results (space-delimited format)")

        btn = self.create_button(log_row, text="Browse...", command=self.browse_log_file)
        btn.pack(side='left')
        self.add_tooltip(btn, "Browse to select log file location")
        
        # Run button - normal button
        self.run_button = self.create_button(export_frame, text="Run batch process", 
                                            command=self.run_batch)
        self.run_button.pack(fill='x', pady=12)
        self.add_tooltip(self.run_button, "Start batch processing of all files in the queue")
        
        # Progress bar - start with determinate mode
        self.progress = ttk.Progressbar(export_frame, mode='determinate', maximum=100, value=0)
        self.progress.pack(fill='x', pady=5)
        
        self.status_var = tk.StringVar(value="Ready")
        lbl = ttk.Label(export_frame, textvariable=self.status_var, background='')
        lbl.pack(pady=5)
        self.add_tooltip(lbl, "Current processing status")

    def open_help_link(self):
        """Open the Google Drive link with TGS theory notes in the default web browser"""
        import webbrowser
        help_url = "https://drive.google.com/drive/folders/1fUQv3WHg6I_4-MKwgp88PSY_l_CJ-JZ6?usp=drive_link"
        webbrowser.open_new_tab(help_url)  # Use open_new_tab instead of open
    
    def build_log_section(self, parent):
        """Right panel - plot preview on top, results table in middle, log output below"""
        self.right_paned = ttk.PanedWindow(parent, orient='vertical')
        self.right_paned.pack(fill='both', expand=True)
        
        # Top frame for plot preview - use a custom frame with title bar
        plot_container = ttk.Frame(self.right_paned)
        self.right_paned.add(plot_container, weight=3)  # Increased weight for plot
        
        # Create title bar for the plot container
        title_bar = ttk.Frame(plot_container)
        title_bar.pack(fill='x', pady=(0, 5))
        
        # Create title label with window background color (darker grey)
        title_label = ttk.Label(title_bar, text="Fit preview", font=('Arial', 10, 'bold'),
                                background=self.bg_color, foreground=self.fg_color)
        title_label.pack(side='left', padx=(12, 0))
        
        # Add Help button to the title bar
        help_btn = self.create_button(title_bar, text="Help", command=self.open_help_link)
        help_btn.pack(side='right', padx=(0, 5))
        self.add_tooltip(help_btn, "How all this works can be found in the TGS Theory Notes here")
        
        # Add save button to the title bar
        save_btn = self.create_button(title_bar, text="Save plot", command=self.save_current_plot)
        save_btn.pack(side='right', padx=(0, 12))
        self.add_tooltip(save_btn, "Save the current plot as PNG, PDF, or SVG")
        
        # Create a frame for the plot content - no border, just panel background
        plot_frame = ttk.Frame(plot_container)
        plot_frame.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        
        # Create matplotlib figure for interactive plotting
        self.preview_fig = Figure(figsize=(8, 5), dpi=100, facecolor=self.panel_bg, tight_layout=True)
        self.preview_ax = self.preview_fig.add_subplot(111)
        self.preview_ax.set_facecolor(self.panel_bg)
        self.preview_ax.tick_params(colors=self.fg_color)
        self.preview_ax.xaxis.label.set_color(self.fg_color)
        self.preview_ax.yaxis.label.set_color(self.fg_color)
        self.preview_ax.title.set_color(self.fg_color)
        for spine in self.preview_ax.spines.values():
            spine.set_color(self.fg_color)

        # Create inset axes for FFT plot (initially hidden)
        self.inset_ax = self.preview_ax.inset_axes([0.65, 0.6, 0.3, 0.35])
        self.inset_ax.set_facecolor(self.panel_bg)
        self.inset_ax.tick_params(colors=self.fg_color)
        self.inset_ax.xaxis.label.set_color(self.fg_color)
        self.inset_ax.yaxis.label.set_color(self.fg_color)
        for spine in self.inset_ax.spines.values():
            spine.set_color(self.fg_color)
        self.inset_ax.set_visible(False)

        self.preview_canvas = FigureCanvasTkAgg(self.preview_fig, master=plot_frame)
        self.preview_canvas.get_tk_widget().pack(fill='both', expand=True, padx=5, pady=5)

        self.preview_canvas.draw_idle()
        self.root.update_idletasks()

        # Store the latest fit data for redrawing
        self.current_fit_data = None
        
        # MIDDLE FRAME: Results table (Fit parameters)
        # Increased weight to give more space to fit parameters
        results_frame = ttk.LabelFrame(self.right_paned, text="Fit parameters", padding=(12, 8))
        self.right_paned.add(results_frame, weight=4)

        # Create Treeview for results table - increased height to fill more space
        self.results_tree = ttk.Treeview(results_frame, columns=('value',), show='tree headings', height=8)  # Increased from 5 to 8
        self.results_tree.heading('#0', text='Parameter')
        self.results_tree.heading('value', text='Value')

        # Configure column widths
        self.results_tree.column('#0', width=200, minwidth=150)
        self.results_tree.column('value', width=200, minwidth=150)

        # Pack the treeview without scrollbar
        self.results_tree.pack(fill='both', expand=True)
        
        # Configure treeview colors for dark theme
        style = ttk.Style()
        style.configure("Treeview", background=self.entry_bg, foreground=self.fg_color, fieldbackground=self.entry_bg)
        style.configure("Treeview.Heading", background=self.button_color, foreground=self.fg_color)
        style.map('Treeview', background=[('selected', self.select_color)])
        
        # ===== BOTTOM FRAME: Log output with custom dark scrollbar =====
        # Reduced weight to make log pane about 2/3 of its previous height
        log_frame = ttk.LabelFrame(self.right_paned, text="Log output", padding=(12, 8))
        self.right_paned.add(log_frame, weight=5)

        # Create a frame to hold the text widget and custom scrollbar
        log_container = ttk.Frame(log_frame)
        log_container.pack(fill='both', expand=True, padx=5, pady=5)
        
        # Create the text widget without its own scrollbar - reduced height
        self.log_text = tk.Text(
            log_container,
            wrap=tk.WORD,
            height=6,  # Reduced from default to give less space
            bg='#252526',
            fg='#d4d4d4',
            insertbackground='#f0f0f0',
            selectbackground='#264f78',
            selectforeground='#f0f0f0',
            relief='flat',
            borderwidth=0,
            font=('Consolas', 9)
        )
        self.log_text.pack(side='left', fill='both', expand=True)
        
        # Configure ttk scrollbar style for dark theme
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=self.entry_bg,
            troughcolor=self.bg_color,
            bordercolor=self.bg_color,
            arrowcolor=self.fg_color,
            lightcolor=self.entry_bg,
            darkcolor=self.entry_bg,
            relief='flat',
            borderwidth=0
        )
        
        # Map hover states for scrollbar
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[('active', self.select_color), ('pressed', self.select_color)],
            arrowcolor=[('active', 'white'), ('pressed', 'white')]
        )
        
        # Create the custom scrollbar
        scrollbar = ttk.Scrollbar(
            log_container,
            orient='vertical',
            command=self.log_text.yview,
            style='Dark.Vertical.TScrollbar'
        )
        scrollbar.pack(side='right', fill='y')
        
        # Configure the text widget to use the scrollbar
        self.log_text.config(yscrollcommand=scrollbar.set)
        
        # Button frame for log controls - right-aligned with proper spacing
        btn_frame = ttk.Frame(log_frame)
        btn_frame.pack(fill='x', pady=(8, 0))
        
        # Use a container with right alignment and consistent button sizing
        btn_container = ttk.Frame(btn_frame)
        btn_container.pack(side='right')
        
        # Clear Log button - with consistent spacing
        btn_clear = self.create_button(btn_container, text="Clear Log", command=self.clear_log)
        btn_clear.pack(side='left', padx=(0, 10))
        self.add_tooltip(btn_clear, "Clear the log output display")
        
        # Save Log button
        btn_save = self.create_button(btn_container, text="Save Log", command=self.save_log)
        btn_save.pack(side='left')
        self.add_tooltip(btn_save, "Save the entire log output to a text file")
    
    def save_log(self):
            """Save the log text widget content to a file."""
            from datetime import datetime
            # Get all text from the log widget
            log_content = self.log_text.get(1.0, tk.END).strip()
            if not log_content:
                self.log_message("Log is empty, nothing to save.", logging.WARNING)
                return

            # Suggest a default filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_filename = f"log_output_{timestamp}.txt"

            file_path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                initialfile=default_filename,
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                title="Save Log As"
            )
            if not file_path:
                return

            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(log_content)
                self.log_message(f"Log saved to: {file_path}")
            except Exception as e:
                self.log_message(f"Failed to save log: {str(e)}", logging.ERROR)        

    def save_current_plot(self):
        """Save the currently displayed plot to a file"""
        if not hasattr(self, 'current_fit_data') or self.current_fit_data is None:
            # Try to get the currently selected fit data
            selection = self.batch_listbox.curselection()
            if not selection:
                self.log_message("No plot available. Please select a processed run first.", logging.WARNING)
                return
            
            idx = selection[0]
            if idx >= len(self.pos_files):
                self.log_message("No plot available.", logging.WARNING)
                return
            
            pos_file = self.pos_files[idx]
            file_id = Path(pos_file).stem
            
            if file_id in self.file_to_fit_plot_data:
                fit_data = self.file_to_fit_plot_data[file_id]
            else:
                messagebox.showwarning("Warning", "No plot data available for the selected run.")
                return
        else:
            fit_data = self.current_fit_data
        
        # Ask user for save location
        file_path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[
                ("PNG image", "*.png"),
                ("PDF document", "*.pdf"),
                ("SVG image", "*.svg"),
                ("All files", "*.*")
            ],
            title="Save plot as"
        )
        
        if not file_path:
            return  # User cancelled
        
        try:
            # Create a new figure for saving with higher DPI
            save_fig = Figure(figsize=(8, 5), dpi=300, facecolor=self.panel_bg, tight_layout=True)
            save_ax = save_fig.add_subplot(111)
            save_ax.set_facecolor(self.panel_bg)
            
            # Style all spines (borders) to match the dark theme
            for spine in save_ax.spines.values():
                spine.set_color(self.fg_color)
                spine.set_linewidth(1)
            
            # Copy the current plot to the new figure
            font_props = {'family': 'Arial', 'color': self.fg_color, 'size': 11}
            tick_font_props = {'family': 'Arial', 'color': self.fg_color, 'size': 9}
            
            # Plot raw data
            if 'time_raw' in fit_data and 'signal_raw' in fit_data:
                save_ax.plot(fit_data['time_raw'] * 1e9, fit_data['signal_raw'] * 1e3, 
                            '-', color='white', linewidth=0.75, alpha=0.7, label='Raw Data', zorder=1)
            
            # Plot functional fit
            if 'time_fit' in fit_data and 'fit_signal' in fit_data:
                save_ax.plot(fit_data['time_fit'] * 1e9, fit_data['fit_signal'] * 1e3, 
                            '-', color='#4dabf7', linewidth=1, label='Functional Fit', alpha=0.9, zorder=2)
            
            # Plot thermal fit
            if 'time_fit' in fit_data and 'thermal_signal' in fit_data:
                save_ax.plot(fit_data['time_fit'] * 1e9, fit_data['thermal_signal'] * 1e3, 
                            '-', color='#ff6b6b', linewidth=1, label='Thermal Fit', alpha=0.9, zorder=3)
            
            # Set labels and title with Arial font
            save_ax.set_xlabel('Time [ns]', fontdict=font_props)
            save_ax.set_ylabel('Signal Amplitude [mV]', fontdict=font_props)
            if 'title' in fit_data:
                save_ax.set_title(fit_data['title'], fontdict={'family': 'Arial', 'color': self.fg_color, 'size': 12}, pad=10)
            
            save_ax.grid(True, linestyle='--', linewidth=0.5, alpha=0.3, color=self.fg_color)
            
            # Style tick labels
            save_ax.tick_params(colors=self.fg_color, labelsize=9)
            for label in save_ax.get_xticklabels():
                label.set_fontfamily('Arial')
                label.set_fontsize(9)
                label.set_color(self.fg_color)
            for label in save_ax.get_yticklabels():
                label.set_fontfamily('Arial')
                label.set_fontsize(9)
                label.set_color(self.fg_color)
            
            # Legend
            legend = save_ax.legend(loc='lower right', fontsize=9, 
                                facecolor=self.panel_bg, edgecolor=self.fg_color)
            legend.get_frame().set_alpha(0.8)
            for text in legend.get_texts():
                text.set_color('white')
                text.set_fontfamily('Arial')
            
            # Add inset FFT if available
            has_fft = ('fft_freq' in fit_data and fit_data['fft_freq'] is not None and 
                    len(fit_data['fft_freq']) > 0 and 'fft_amp' in fit_data and fit_data['fft_amp'] is not None)
            
            if has_fft:
                # Create inset axes
                inset_ax = save_ax.inset_axes([0.6, 0.55, 0.35, 0.4])
                inset_ax.set_facecolor(self.panel_bg)
                
                # Configure inset spines
                for spine in inset_ax.spines.values():
                    spine.set_color(self.fg_color)
                    spine.set_linewidth(1)
                
                # Set tick colors and font
                inset_ax.tick_params(colors=self.fg_color, labelsize=8)
                for label in inset_ax.get_xticklabels():
                    label.set_fontfamily('Arial')
                    label.set_fontsize(8)
                    label.set_color(self.fg_color)
                for label in inset_ax.get_yticklabels():
                    label.set_fontfamily('Arial')
                    label.set_fontsize(8)
                    label.set_color(self.fg_color)
                
                # Plot FFT data
                fft_freq = fit_data['fft_freq']
                fft_amp = fit_data['fft_amp']
                
                valid_mask = np.isfinite(fft_freq) & np.isfinite(fft_amp)
                if np.any(valid_mask):
                    fft_freq_valid = fft_freq[valid_mask]
                    fft_amp_valid = fft_amp[valid_mask]
                    
                    inset_ax.plot(fft_freq_valid, fft_amp_valid, 
                                '-', color='white', linewidth=0.5, alpha=0.7, label='FFT')
                    
                    # Plot Lorentzian fit if available
                    if ('lorentzian_fit' in fit_data and fit_data['lorentzian_fit'] is not None and
                        'lorentzian_freq' in fit_data and fit_data['lorentzian_freq'] is not None):
                        lorentz_freq = fit_data['lorentzian_freq']
                        lorentz_fit = fit_data['lorentzian_fit']
                        
                        valid_lorentz = np.isfinite(lorentz_freq) & np.isfinite(lorentz_fit)
                        if np.any(valid_lorentz):
                            inset_ax.plot(lorentz_freq[valid_lorentz], lorentz_fit[valid_lorentz], 
                                        '-', color='#ff6b6b', linewidth=0.75, alpha=0.9, label='Lorentzian')
                    
                    # --- MODIFIED: Set axis limits based on user's frequency bounds ---
                    # Get frequency bounds from config
                    freq_bounds = self.config.get('lorentzian', {}).get('frequency_bounds', [0.1, 0.9])
                    
                    # Add 0.1 GHz padding on each side
                    xmin = max(0.0, freq_bounds[0] - 0.1)
                    xmax = freq_bounds[1] + 0.1
                    
                    inset_ax.set_xlim(xmin, xmax)
                    # --- END MODIFIED ---
                    
                    inset_ax.set_xlabel('Frequency [GHz]', fontsize=8, fontfamily='Arial', color=self.fg_color)
                    inset_ax.set_ylabel('Intensity', fontsize=8, fontfamily='Arial', color=self.fg_color)
                    inset_ax.grid(True, linestyle='--', linewidth=0.5, alpha=0.3, color=self.fg_color)
                    
                    inset_legend = inset_ax.legend(loc='upper right', fontsize=6,
                                                facecolor=self.panel_bg, edgecolor=self.fg_color)
                    inset_legend.get_frame().set_alpha(0.8)
                    for text in inset_legend.get_texts():
                        text.set_color('white')
                        text.set_fontfamily('Arial')
            
            # Save the figure
            save_fig.savefig(file_path, dpi=300, bbox_inches='tight', facecolor=self.panel_bg)
            plt.close(save_fig)
            
            self.log_message(f"Plot saved to: {file_path}")
            #messagebox.showinfo("Success", f"Plot saved successfully to:\n{file_path}")
            
        except Exception as e:
            self.log_message(f"Error saving plot: {str(e)}", logging.ERROR)
            #messagebox.showerror("Error", f"Failed to save plot:\n{str(e)}")
    
    def create_interactive_plot(self, fit_data):
        """Create an interactive plot with main TGS curve and inset FFT"""
        
        # Store current fit data for saving
        self.current_fit_data = fit_data

        # --- CRITICAL: Clear the inset axes completely ---
        if hasattr(self, 'inset_ax'):
            try:
                self.inset_ax.remove()
                del self.inset_ax
            except:
                pass
        
        # Clear the main axes
        self.preview_ax.clear()
        self.preview_ax.set_facecolor(self.panel_bg)
        
        # Set font properties for all text - Arial throughout
        font_props = {'family': 'Arial', 'color': self.fg_color}
        
        # Set tick params
        self.preview_ax.tick_params(colors=self.fg_color, labelsize=9)
        for label in self.preview_ax.get_xticklabels():
            label.set_fontfamily('Arial')
            label.set_fontsize(9)
            label.set_color(self.fg_color)
        for label in self.preview_ax.get_yticklabels():
            label.set_fontfamily('Arial')
            label.set_fontsize(9)
            label.set_color(self.fg_color)
        
        # Plot raw data
        if 'time_raw' in fit_data and 'signal_raw' in fit_data:
            self.preview_ax.plot(fit_data['time_raw'] * 1e9, fit_data['signal_raw'] * 1e3, 
                                '-', color='white', linewidth=0.75, alpha=0.7, label='Raw Data', zorder=1)
        
        # Plot functional fit
        if 'time_fit' in fit_data and 'fit_signal' in fit_data:
            self.preview_ax.plot(fit_data['time_fit'] * 1e9, fit_data['fit_signal'] * 1e3, 
                                '-', color='#4dabf7', linewidth=1, label='Functional Fit', alpha=0.9, zorder=2)
        
        # Plot thermal fit
        if 'time_fit' in fit_data and 'thermal_signal' in fit_data:
            self.preview_ax.plot(fit_data['time_fit'] * 1e9, fit_data['thermal_signal'] * 1e3, 
                                '-', color='#ff6b6b', linewidth=1, label='Thermal Fit', alpha=0.9, zorder=3)
        
        # Set labels and title with Arial font
        self.preview_ax.set_xlabel('Time [ns]', fontdict=font_props, fontsize=11)
        self.preview_ax.set_ylabel('Signal Amplitude [mV]', fontdict=font_props, fontsize=11)
        if 'title' in fit_data:
            self.preview_ax.set_title(fit_data['title'], fontdict=font_props, fontsize=12, pad=10)
        
        self.preview_ax.grid(True, linestyle='--', linewidth=0.5, alpha=0.3, color=self.fg_color)
        
        # Legend moved to bottom right with scaled size (0.7x)
        legend = self.preview_ax.legend(loc='lower right', fontsize=9, 
                                    facecolor=self.panel_bg, edgecolor=self.fg_color)
        legend.get_frame().set_alpha(0.8)
        for text in legend.get_texts():
            text.set_color('white')
            text.set_fontfamily('Arial')
            text.set_fontsize(int(9 * 0.7))  # Scale legend text by 0.7
        
        # Handle inset FFT plot - Remove and recreate inset each time to ensure it works
        # First, remove any existing inset
        if hasattr(self, 'inset_ax'):
            try:
                self.inset_ax.remove()
            except:
                pass
        
        # Check if we have FFT data to display
        has_fft = ('fft_freq' in fit_data and fit_data['fft_freq'] is not None and 
                len(fit_data['fft_freq']) > 0 and 'fft_amp' in fit_data and fit_data['fft_amp'] is not None)
        
        if has_fft:
            # Create a new inset axes - 1.1x larger than before (was [0.6, 0.55, 0.35, 0.4], now scaled by 1.1)
            # New size: width 0.35*1.1=0.385, height 0.4*1.1=0.44
            self.inset_ax = self.preview_ax.inset_axes([0.59, 0.53, 0.385, 0.44])
            self.inset_ax.set_facecolor(self.panel_bg)
            
            # Configure inset spines
            for spine in self.inset_ax.spines.values():
                spine.set_color(self.fg_color)
                spine.set_linewidth(1)
            
            # Set tick colors and Arial font
            self.inset_ax.tick_params(colors=self.fg_color, labelsize=9)
            for label in self.inset_ax.get_xticklabels():
                label.set_fontfamily('Arial')
                label.set_fontsize(9)
                label.set_color(self.fg_color)
            for label in self.inset_ax.get_yticklabels():
                label.set_fontfamily('Arial')
                label.set_fontsize(9)
                label.set_color(self.fg_color)
            
            # Plot FFT data
            fft_freq = fit_data['fft_freq']
            fft_amp = fit_data['fft_amp']

            # Filter valid data
            valid_mask = np.isfinite(fft_freq) & np.isfinite(fft_amp)
            if np.any(valid_mask):
                fft_freq_valid = fft_freq[valid_mask]
                fft_amp_valid = fft_amp[valid_mask]
                
                # Plot FFT
                self.inset_ax.plot(fft_freq_valid, fft_amp_valid, 
                                '-', color='white', linewidth=0.5, alpha=0.7, label='FFT')
                
                # Plot Lorentzian fit if available (line thickness reduced by half: was 1.5, now 0.75)
                if ('lorentzian_fit' in fit_data and fit_data['lorentzian_fit'] is not None and
                    'lorentzian_freq' in fit_data and fit_data['lorentzian_freq'] is not None):
                    lorentz_freq = fit_data['lorentzian_freq']
                    lorentz_fit = fit_data['lorentzian_fit']
                    
                    # Ensure we have valid data
                    valid_lorentz = np.isfinite(lorentz_freq) & np.isfinite(lorentz_fit)
                    if np.any(valid_lorentz):
                        lorentz_freq_valid = lorentz_freq[valid_lorentz]
                        lorentz_fit_valid = lorentz_fit[valid_lorentz]
                        
                        # Plot Lorentzian fit with reduced line thickness (0.75 instead of 1.5)
                        self.inset_ax.plot(lorentz_freq_valid, lorentz_fit_valid, 
                                        '-', color='#ff6b6b', linewidth=0.75, alpha=0.9, label='Lorentzian Fit')
                
                # Get frequency bounds from config
                freq_bounds = self.config.get('lorentzian', {}).get('frequency_bounds', [0.1, 0.9])
                
                # Add 0.1 GHz padding on each side
                xmin = max(0.0, freq_bounds[0] - 0.1)
                xmax = freq_bounds[1] + 0.1
                
                self.inset_ax.set_xlim(xmin, xmax)
                
                # Set labels with Arial font
                self.inset_ax.set_xlabel('Frequency [GHz]', fontsize=9, fontfamily='Arial', color=self.fg_color)
                self.inset_ax.set_ylabel('Intensity', fontsize=9, fontfamily='Arial', color=self.fg_color)
                self.inset_ax.grid(True, linestyle='--', linewidth=0.5, alpha=0.3, color=self.fg_color)
                
                # Inset legend with Arial font (scaled by 0.7: was 7, now 5)
                inset_legend = self.inset_ax.legend(loc='upper right', fontsize=5,
                                                facecolor=self.panel_bg, edgecolor=self.fg_color)
                inset_legend.get_frame().set_alpha(0.8)
                for text in inset_legend.get_texts():
                    text.set_color('white')
                    text.set_fontfamily('Arial')
                    text.set_fontsize(5)
                
                self.inset_ax.set_zorder(10)

        # Adjust layout and redraw
        self.preview_fig.tight_layout()
        self.preview_canvas.draw_idle()

    def log_detailed_memory(self, label=""):
        """Log detailed memory usage including matplotlib figure count and gc stats"""
        import gc
        
        # Get matplotlib figure count
        import matplotlib.pyplot as plt
        fig_count = len(plt.get_fignums())
        
        # Get gc stats
        gc_collectable = gc.get_count()
        
        # Get object counts for key types
        obj_counts = {}
        for type_name in ['DataFrame', 'Series', 'ndarray']:
            try:
                if type_name == 'ndarray':
                    import numpy as np
                    obj_counts[type_name] = sum(1 for obj in gc.get_objects() if isinstance(obj, np.ndarray))
                else:
                    obj_counts[type_name] = sum(1 for obj in gc.get_objects() if type(obj).__name__ == type_name)
            except:
                pass
        
        mem = self.get_memory_usage()
        
        msg = (f"[DETAILED_MEMORY] {label}: "
            f"RSS={mem['rss']:.1f}MB, VMS={mem['vms']:.1f}MB, "
            f"figures={fig_count}, gc={gc_collectable}, "
            f"ndarrays={obj_counts.get('ndarray', 0)}")
        
        print(msg)
        self.log_message(msg, logging.INFO)
        
        # Log which figures exist
        if fig_count > 10:
            fig_nums = plt.get_fignums()
            fig_info = []
            for num in fig_nums[:5]:  # First 5 only
                try:
                    fig = plt.figure(num)
                    fig_info.append(f"#{num}:{fig.get_size_inches()}")
                except:
                    pass
            print(f"[DETAILED_MEMORY] Open figures: {fig_info}")

    def format_value_with_error(self, value, error, conversion_factor=1.0):
        """Format a value with its error using ± symbol, handle inf/NaN"""
        # Check if error is inf or NaN
        if error is None or np.isinf(error) or np.isnan(error):
            return "Bad fit"
        
        # Check if value is inf or NaN
        if value is None or np.isinf(value) or np.isnan(value):
            return "Bad fit"
        
        try:
            # Convert to float and apply conversion factor
            val = float(value) * conversion_factor
            err = float(error) * conversion_factor
            
            if err == 0:
                return f"{val:.6e}"
            
            # Determine appropriate significant figures
            if abs(val) < 0.001 or abs(val) > 1000:
                # Use scientific notation
                err_str = f"{err:.2e}"
                if 'e' in err_str:
                    exp = int(err_str.split('e')[-1])
                    val_scaled = val / (10**exp)
                    err_scaled = err / (10**exp)
                    if err_scaled < 10:
                        decimals = max(2, 2 - int(np.floor(np.log10(abs(err_scaled)))) + 1)
                    else:
                        decimals = 0
                    return f"{val_scaled:.{decimals}f} ± {err_scaled:.{decimals}f}e{exp}"
            else:
                # For numbers in reasonable range
                if err < 0.0001:
                    decimals = 6
                elif err < 0.001:
                    decimals = 5
                elif err < 0.01:
                    decimals = 4
                elif err < 0.1:
                    decimals = 3
                elif err < 1:
                    decimals = 2
                else:
                    decimals = 1
                
                return f"{val:.{decimals}f} ± {err:.{decimals}f}"
        except:
            return "Bad fit"
    
    def update_results_table(self, fit_params):
        """Update the results table with fit parameters using Treeview"""
        # Check if results_tree exists
        if not hasattr(self, 'results_tree'):
            return
        
        # Clear existing items
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        
        # Define parameters to display with their labels and units
        # Format: (key, display_name, unit, conversion_factor)
        parameters = [
            ('A', 'A, amplitude', 'W·m⁻²', 1.0),
            ('B', 'B, amplitude', 'W·m⁻²', 1.0),
            ('C', 'C, offset', 'W·m⁻²', 1.0),
            ('alpha', 'α, thermal diffusivity', 'mm²·s⁻¹', 1000000.0),
            ('beta', 'β, displacement-reflectance ratio', 's⁰·⁵', 1.0),
            ('theta', 'θ, acoustic phase', 'rad', 1.0),
            ('tau', 'τ, acoustic decay time', 'ns', 1000000000.0),
            ('f', 'f, SAW frequency', 'MHz', 1e-6),
        ]
        
        # Add parameters to the tree
        for key, display_name, unit, conv_factor in parameters:
            if key in fit_params and fit_params[key] is not None:
                value = fit_params[key]
                error_key = f"{key}_err"
                error = fit_params.get(error_key, None)
                formatted_value = self.format_value_with_error(value, error, conv_factor)
                
                # Format the display string with proper superscripts
                # Replace ^-1 with ⁻¹, ^0.5 with ⁰·⁵, etc.
                display_unit = unit.replace('^-1', '⁻¹').replace('^0.5', '⁰·⁵')
                display_text = f"{display_name} [{display_unit}]"
                
                # Insert into tree
                self.results_tree.insert('', 'end', text=display_text, values=(formatted_value,))
            else:
                display_unit = unit.replace('^-1', '⁻¹').replace('^0.5', '⁰·⁵')
                display_text = f"{display_name} [{display_unit}]"
                self.results_tree.insert('', 'end', text=display_text, values=('Not available',))

    def on_batch_item_selected(self, event):
        """Handle selection of a batch list item - display its fitted plot and parameters"""
        selection = self.batch_listbox.curselection()
        if not selection:
            return
        
        idx = selection[0]
        if idx >= len(self.pos_files):
            return
        
        pos_file = self.pos_files[idx]
        full_stem = Path(pos_file).stem
        file_id = full_stem
        
        if file_id not in self.processed_files:
            self._show_placeholder_text("Select a processed run")
            self.clear_results_table()
            return
        
        # Force a fresh regeneration of plot data from disk
        fit_data = self.load_plot_data_from_disk(file_id, pos_file)
        
        if fit_data is not None and 'time_raw' in fit_data and 'signal_raw' in fit_data:
            # Make sure we have the fit parameters
            if file_id in self.file_to_fit_params:
                self.update_results_table(self.file_to_fit_params[file_id])
            
            # Display the plot
            self.create_interactive_plot(fit_data)
        else:
            self._show_placeholder_text(f"No fit data available for\n{file_id}")
            self.clear_results_table()
    
    def cleanup_plot_cache(self):
        """Clear the plot data cache to free memory"""
        if hasattr(self, 'file_to_fit_plot_data'):
            # Keep only the last 3 plots in memory
            keys = list(self.file_to_fit_plot_data.keys())
            if len(keys) > 3:
                for key in keys[:-3]:
                    del self.file_to_fit_plot_data[key]
                import gc
                gc.collect()
                print(f"[MEMORY] Cleaned plot cache, kept {len(self.file_to_fit_plot_data)} plots")

    def display_selected_plot(self, plot_path, file_id, idx):
        """Display selected fit data as an interactive plot with inset FFT (fallback)"""
        try:
            # First check if we have stored plot data
            if hasattr(self, 'file_to_fit_plot_data') and file_id in self.file_to_fit_plot_data:
                fit_data = self.file_to_fit_plot_data[file_id]
                self.create_interactive_plot(fit_data)
                return
            
            # If not, try to get from file_id in fit_params
            if file_id in self.file_to_fit_params:
                fit_params = self.file_to_fit_params[file_id]
                
                # We need to reconstruct the fit data. This requires loading the original signal
                # and the fit results. Since the fit parameters are stored, we can regenerate the fits.
                pos_file = self.pos_files[idx]
                data_dir = Path(pos_file).parent
                
                # Try to load the signal data from the fit directory
                signal_path = data_dir / 'fit' / 'signal.json'
                fit_csv_path = data_dir / 'fit' / 'fit.csv'
                
                fit_data = {'title': file_id}
                
                # Load the signal data
                if signal_path.exists():
                    import json
                    with open(signal_path, 'r') as f:
                        signals = json.load(f)
                    # Find the signal for this file (by index)
                    if idx < len(signals):
                        signal_array = np.array(signals[idx])
                        fit_data['time_raw'] = signal_array[:, 0]
                        fit_data['signal_raw'] = signal_array[:, 1]
                
                # Load the fit parameters to regenerate the fits
                if fit_csv_path.exists():
                    import pandas as pd
                    df = pd.read_csv(fit_csv_path)
                    # Find the row for this file - search by file_id which is the run_name in the CSV
                    row = df[df['run_name'].str.contains(file_id)]
                    if not row.empty:
                        # Extract parameters
                        A = row['A[Wm^-2]'].values[0] if 'A[Wm^-2]' in row else fit_params.get('A', 0)
                        B = row['B[Wm^-2]'].values[0] if 'B[Wm^-2]' in row else fit_params.get('B', 0)
                        C = row['C[Wm^-2]'].values[0] if 'C[Wm^-2]' in row else fit_params.get('C', 0)
                        alpha = row['alpha[m^2s^-1]'].values[0] if 'alpha[m^2s^-1]' in row else fit_params.get('alpha', 1e-6)
                        beta = row['beta[s^0.5]'].values[0] if 'beta[s^0.5]' in row else fit_params.get('beta', 0)
                        theta = row['theta[rad]'].values[0] if 'theta[rad]' in row else fit_params.get('theta', 0)
                        tau = row['tau[s]'].values[0] if 'tau[s]' in row else fit_params.get('tau', 1e-6)
                        f = row['f[Hz]'].values[0] if 'f[Hz]' in row else fit_params.get('f', 1e6)
                        start_time = row['start_time'].values[0] if 'start_time' in row else 0
                        grating_spacing = row['grating_spacing[µm]'].values[0] if 'grating_spacing[µm]' in row else 6.4
                        
                        # Generate time points for fit
                        if 'time_raw' in fit_data:
                            time_raw = fit_data['time_raw']
                            # Use the same time range as raw data but starting from start_time
                            time_fit = np.linspace(max(start_time, time_raw[0]), time_raw[-1], 1000)
                            
                            # Create fit functions
                            from src.analysis.functions import tgs_function
                            functional_func, thermal_func = tgs_function(start_time, grating_spacing)
                            
                            # Generate fits
                            fit_data['time_fit'] = time_fit
                            fit_data['fit_signal'] = functional_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
                            fit_data['thermal_signal'] = thermal_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
                
                # Try to load FFT data
                fft_data_path = data_dir / 'figures' / 'fft-lorentzian' / f'fft-lorentzian-{file_id}.png'
                # For FFT data, we need to reconstruct from the fit parameters
                if 'f' in fit_params and fit_params['f'] is not None:
                    # Generate a synthetic FFT based on the Lorentzian fit
                    freq_range = np.linspace(0.05, 0.95, 500)
                    from src.analysis.functions import lorentzian_function
                    f_ghz = fit_params['f'] / 1e9 if fit_params['f'] is not None else 0.5
                    # Estimate width from tau
                    width = 1 / (2 * np.pi * fit_params.get('tau', 1e-6) * 1e9) if fit_params.get('tau') else 0.05
                    fit_data['fft_freq'] = freq_range
                    fit_data['fft_amp'] = lorentzian_function(freq_range, 1.0, f_ghz, width, 0)
                    fit_data['lorentzian_freq'] = freq_range
                    fit_data['lorentzian_fit'] = lorentzian_function(freq_range, 1.0, f_ghz, width, 0)
                
                # Create the interactive plot
                self.create_interactive_plot(fit_data)
                pass
            else:
                self._show_placeholder_text(f"No fit data available\n{file_id}")
                    
        except Exception as e:
            self.log_message(f"Could not create interactive plot: {str(e)}", logging.DEBUG)
            self._show_placeholder_text(f"Error creating plot\n{file_id}")

    def clear_results_table(self):
        """Clear the results table and show placeholder message"""
        # Check if results_tree exists before trying to use it
        if hasattr(self, 'results_tree'):
            # Clear existing items
            for item in self.results_tree.get_children():
                self.results_tree.delete(item)
            
            # Insert placeholder message
            self.results_tree.insert('', 'end', text='Select a processed run to view fit parameters', values=('',))
        
        # Check if preview_ax exists before trying to clear the plot
        if hasattr(self, 'preview_ax'):
            # Clear the plot and show placeholder
            self.preview_ax.clear()
            self.preview_ax.set_facecolor(self.panel_bg)
            self.preview_ax.text(0.5, 0.5, 'Select a processed run\nto view fit results',
                                ha='center', va='center', transform=self.preview_ax.transAxes,
                                color=self.fg_color, fontsize=12, fontfamily='Arial')
            self.preview_ax.set_xlim(0, 1)
            self.preview_ax.set_ylim(0, 1)
            self.preview_ax.set_xticks([])
            self.preview_ax.set_yticks([])
            
            if hasattr(self, 'inset_ax'):
                self.inset_ax.set_visible(False)
            
            if hasattr(self, 'preview_canvas'):
                self.preview_canvas.draw()

    def remove_selected_item(self):
        """Remove the selected item from the batch queue and from the results log"""
        selection = self.batch_listbox.curselection()
        if not selection:
            self.log_message("No item selected to remove", logging.WARNING)
            return
        
        idx = selection[0]
        
        # Get the file ID before removal
        if idx < len(self.pos_files):
            pos_file = self.pos_files[idx]
            full_stem = Path(pos_file).stem
            file_id = full_stem
            
            # Remove from file lists
            self.pos_files.pop(idx)
            self.neg_files.pop(idx)
            
            # Remove from processed set if present
            if file_id in self.processed_files:
                self.processed_files.remove(file_id)
            
            # Remove from plot mapping if exists
            if file_id in self.file_to_plot_path:
                del self.file_to_plot_path[file_id]
            
            # Remove from fit parameters mapping if exists
            if file_id in self.file_to_fit_params:
                del self.file_to_fit_params[file_id]
            
            # Remove from fit plot data if exists
            if file_id in self.file_to_fit_plot_data:
                del self.file_to_fit_plot_data[file_id]
            
            # Update the listbox
            self._update_batch_listbox_processed_status()
            
            # Remove corresponding entry from results log if it exists
            self.remove_from_results_log(file_id)
            
            self.log_message(f"Removed '{file_id}' from queue and results log")
            self.root.after(0, self.update_summary_plot)
            
            # Clear preview if this was the selected item
            if self.batch_listbox.size() == 0:
                self._show_placeholder_text("Queue empty\nAdd files to process")
                self.clear_results_table()
            elif idx == 0 and self.batch_listbox.size() > 0:
                # Select the first item if it exists
                self.batch_listbox.selection_set(0)
                self.on_batch_item_selected(None)
        else:
            self.batch_listbox.delete(idx)
            self.log_message("Removed item from queue")
    
    def remove_from_results_log(self, file_id):
        """Remove a row corresponding to file_id from the results log file"""
        # Check if we have a current results log path
        log_path = self.log_file_var.get().strip()
        if not log_path:
            # Try to determine if there's a default log file
            if self.pos_files:
                first_file = Path(self.pos_files[0])
                default_log = first_file.parent / f"{first_file.stem.split('-POS')[0]}_postprocessing.txt"
                if default_log.exists():
                    log_path = str(default_log)
                else:
                    return
            else:
                return
        
        log_file = Path(log_path)
        if not log_file.exists():
            return
        
        try:
            # Read all lines from the log file
            with open(log_file, 'r') as f:
                lines = f.readlines()
            
            if len(lines) <= 1:
                return  # Only header or empty
            
            # Header is first line
            header = lines[0]
            data_lines = lines[1:]
            
            # Find and remove lines that start with the file_id (run_name)
            new_data_lines = []
            removed = False
            
            for line in data_lines:
                # Check if the first word (run_name) matches file_id
                parts = line.strip().split()
                if parts and parts[0] == file_id:
                    removed = True
                    self.log_message(f"Removed '{file_id}' from results log")
                else:
                    new_data_lines.append(line)
            
            # Write back the file without the removed line
            if removed:
                with open(log_file, 'w') as f:
                    f.write(header)
                    f.writelines(new_data_lines)
                self.log_message(f"Updated results log: {log_file}")
            else:
                self.log_message(f"File '{file_id}' not found in results log", logging.DEBUG)
                
        except Exception as e:
            self.log_message(f"Error updating results log: {str(e)}", logging.WARNING)

    def cleanup_old_plots(self, data_dir, max_to_keep=50):
        """Delete old combined plot images to save disk space (optional)"""
        try:
            combined_dir = data_dir / 'figures' / 'combined'
            if combined_dir.exists():
                # Get all combined plot files sorted by modification time
                plot_files = sorted(combined_dir.glob('combined-*.png'), key=lambda x: x.stat().st_mtime)
                
                # Delete older files beyond max_to_keep
                for old_file in plot_files[:-max_to_keep]:
                    try:
                        old_file.unlink()
                    except:
                        pass
        except:
            pass  # Silent fail for cleanup

    def _show_placeholder_text(self, text):
        """Show placeholder text when no image is available"""
        self.preview_ax.clear()
        self.preview_ax.set_facecolor(self.panel_bg)
        
        # Handle the specific "Select a processed run" message
        if text == "Select a processed run":
            self.preview_ax.text(0.5, 0.5, text,
                                ha='center', va='center', transform=self.preview_ax.transAxes,
                                color='#ff6b6b', fontsize=14, fontfamily='Arial', weight='bold')
        else:
            self.preview_ax.text(0.5, 0.5, text,
                                ha='center', va='center', transform=self.preview_ax.transAxes,
                                color=self.fg_color, fontsize=12, fontfamily='Arial', wrap=True)
        
        self.preview_ax.set_xlim(0, 1)
        self.preview_ax.set_ylim(0, 1)
        self.preview_ax.set_xticks([])
        self.preview_ax.set_yticks([])
        self.preview_canvas.draw()

    def open_config_editor(self):
        """Open a scrollable window to edit all config parameters with intuitive controls"""
        editor_window = tk.Toplevel(self.root)
        editor_window.title("Fitting parameters")
        
        # Let the window size to its content, but set a reasonable default
        editor_window.geometry("")  # Clear any forced geometry
        editor_window.update_idletasks()  # Update to get natural size
        # Set minimum size to prevent it from being too small
        editor_window.minsize(1200, 720)
        
        # Apply dark theme to editor window
        editor_window.configure(background=self.bg_color)
        
        # Make it modal
        editor_window.transient(self.root)
        editor_window.grab_set()
        
        # Create main frame
        main_frame = ttk.Frame(editor_window)
        main_frame.pack(fill='both', expand=True, padx=10, pady=10)
        
        # Create a canvas with scrollbar for the entire content
        canvas = tk.Canvas(main_frame, background=self.bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw", width=canvas.winfo_reqwidth())
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Update canvas width when frame size changes
        def update_canvas_width(event):
            canvas.itemconfig(1, width=event.width)
        
        canvas.bind('<Configure>', update_canvas_width)
        
        # Store widgets for saving
        self.config_widgets = {}
        
        # Create a container frame for the two columns
        columns_frame = ttk.Frame(scrollable_frame)
        columns_frame.pack(fill='both', expand=True, padx=10, pady=10)
        
        # Create left and right column frames with equal width distribution
        left_column = ttk.Frame(columns_frame)
        left_column.pack(side='left', fill='both', expand=True, padx=(0, 5))
        
        right_column = ttk.Frame(columns_frame)
        right_column.pack(side='right', fill='both', expand=True, padx=(5, 0))
        
        # Define intuitive configuration sections
        sections = [
            {
                'column': left_column,
                'title': "Path settings",  # Changed from "Path Settings"
                'params': [
                    {
                        'key': 'path',
                        'label': 'Data directory',  # Changed from "Data Directory"
                        'tooltip': 'Directory containing input files',
                        'type': 'text',
                        'width': 45,
                        'default': ''
                    },
                    {
                        'key': 'study_names',
                        'label': 'Study names',  # Changed from "Study Names"
                        'tooltip': 'Study names to fit (comma-separated, leave empty for all)',
                        'type': 'text',
                        'width': 45,
                        'default': ''
                    },
                    {
                        'key': 'idxs',
                        'label': 'File indices',  # Changed from "File Indices"
                        'tooltip': 'Indices of files to fit (comma-separated, leave empty for all)',
                        'type': 'text',
                        'width': 45,
                        'default': ''
                    }
                ]
            },
            {
                'column': left_column,
                'title': "Signal processing",  # Changed from "Signal Processing"
                'params': [
                    {
                        'key': 'signal_process.heterodyne',
                        'label': 'Detection method',  # Changed from "Detection Method"
                        'tooltip': 'Heterodyne detection method for TGS signal',
                        'type': 'select',
                        'options': ['di-homodyne', 'mono-homodyne'],
                        'value_type': str,
                        'default': 'di-homodyne'
                    },
                    {
                        'key': 'signal_process.null_point',
                        'label': 'Null point',  # Changed from "Null Point"
                        'tooltip': 'Null point selection for TGS signal phase analysis (1-4)',
                        'type': 'select',
                        'options': ['1', '2', '3', '4'],
                        'value_type': int,
                        'default': '2'
                    },
                    {
                        'key': 'signal_process.initial_samples',
                        'label': 'Initial samples',  # Changed from "Initial Samples"
                        'tooltip': 'Number of samples for initial correction and prominence calculation',
                        'type': 'number',
                        'value_type': int,
                        'default': 50
                    }
                ]
            },
            {
                'column': left_column,
                'title': "Baseline correction",  # Changed from "Baseline Correction"
                'params': [
                    {
                        'key': 'signal_process.baseline_correction.enabled',
                        'label': 'Enable baseline',  # Changed from "Enable Baseline"
                        'tooltip': 'Enable/disable baseline correction using reference files',
                        'type': 'toggle',
                        'on_text': 'Enabled',
                        'default': False
                    },
                    {
                        'key': 'signal_process.baseline_correction.pos',
                        'label': 'POS reference',  # Changed from "POS Reference"
                        'tooltip': 'Filename for positive reference baseline',
                        'type': 'text',
                        'width': 35,
                        'default': ''
                    },
                    {
                        'key': 'signal_process.baseline_correction.neg',
                        'label': 'NEG reference',  # Changed from "NEG Reference"
                        'tooltip': 'Filename for negative reference baseline',
                        'type': 'text',
                        'width': 35,
                        'default': ''
                    }
                ]
            },
            {
                'column': left_column,
                'title': "FFT analysis",  # Changed from "FFT Analysis"
                'params': [
                    {
                        'key': 'fft.signal_proportion',
                        'label': 'Signal proportion',  # Changed from "Signal Proportion"
                        'tooltip': 'Proportion of signal to analyze (0.0 to 1.0)',
                        'type': 'number',
                        'value_type': float,
                        'default': 1.0
                    },
                    {
                        'key': 'fft.use_derivative',
                        'label': 'Use derivative',  # Changed from "Use Derivative"
                        'tooltip': 'Use signal derivative instead of raw signal',
                        'type': 'toggle',
                        'on_text': 'Enabled',
                        'default': True
                    },
                    {
                        'key': 'fft.analysis_type',
                        'label': 'Analysis type',  # Changed from "Analysis Type"
                        'tooltip': 'Analysis method for frequency domain',
                        'type': 'select',
                        'options': ['psd', 'fft'],
                        'value_type': str,
                        'default': 'psd'
                    }
                ]
            },
            {
                'column': right_column,
                'title': "Lorentzian fitting",  # Changed from "Lorentzian Fitting"
                'params': [
                    {
                        'key': 'lorentzian.signal_proportion',
                        'label': 'Signal proportion',  # Changed from "Signal Proportion"
                        'tooltip': 'Proportion of signal to use for fitting (0.0 to 1.0)',
                        'type': 'number',
                        'value_type': float,
                        'default': 1.0
                    },
                    {
                        'key': 'lorentzian.frequency_bounds',
                        'label': 'Frequency range (GHz)',  # Changed from "Frequency Range (GHz)"
                        'tooltip': 'Frequency range for fitting in GHz [min, max]',
                        'type': 'range',
                        'value_type': float,
                        'default': [0.1, 0.9]
                    },
                    {
                        'key': 'lorentzian.dc_filter_range',
                        'label': 'DC filter range (Hz)',  # Changed from "DC Filter Range (Hz)"
                        'tooltip': 'DC filtering range in Hz [min, max]',
                        'type': 'range',
                        'value_type': int,
                        'default': [0, 50000]
                    },
                    {
                        'key': 'lorentzian.bimodal_fit',
                        'label': 'Two SAW fit',  # Changed from "Two SAW Fit"
                        'tooltip': 'Enable bimodal Lorentzian fitting for two SAW peaks',
                        'type': 'toggle',
                        'on_text': 'Enabled',
                        'default': False
                    },
                    {
                        'key': 'lorentzian.use_skewed_super_lorentzian',
                        'label': 'Skewed super-Lorentzian',  # Changed from "Skewed Super-Lorentzian"
                        'tooltip': 'Use skewed super-Lorentzian for asymmetric peaks',
                        'type': 'toggle',
                        'on_text': 'Enabled',
                        'default': False
                    }
                ]
            },
            {
                'column': right_column,
                'title': "TGS fitting",  # Changed from "TGS Fitting"
                'params': [
                    {
                        'key': 'tgs.grating_spacing',
                        'label': 'Grating spacing (µm)',  # Changed from "Grating Spacing (µm)"
                        'tooltip': 'TGS probe grating spacing in micrometers',
                        'type': 'number',
                        'value_type': float,
                        'default': 3.5276
                    },
                    {
                        'key': 'tgs.signal_proportion',
                        'label': 'Signal proportion',  # Changed from "Signal Proportion"
                        'tooltip': 'Proportion of signal to use for fitting (0.0 to 1.0)',
                        'type': 'number',
                        'value_type': float,
                        'default': 1.0
                    },
                    {
                        'key': 'tgs.maxfev',
                        'label': 'Max iterations',  # Changed from "Max Iterations"
                        'tooltip': 'Maximum number of iterations for final functional fit',
                        'type': 'number',
                        'value_type': int,
                        'default': 1000000
                    }
                ]
            },
            {
                'column': right_column,
                'title': "Plot settings",  # Changed from "Plot Settings"
                'params': [
                    {
                        'key': 'plot.signal_process',
                        'label': 'Plot processed signal',  # Changed from "Plot Processed Signal"
                        'tooltip': 'Enable/disable processed signal visualization',
                        'type': 'toggle',
                        'on_text': 'Show',
                        'default': True
                    },
                    {
                        'key': 'plot.fft_lorentzian',
                        'label': 'Plot FFT & Lorentzian',  # Kept as is (abbreviation)
                        'tooltip': 'Enable/disable FFT and Lorentzian fit visualization',
                        'type': 'toggle',
                        'on_text': 'Show',
                        'default': True
                    },
                    {
                        'key': 'plot.tgs',
                        'label': 'Plot TGS fit',  # Changed from "Plot TGS Fit"
                        'tooltip': 'Enable/disable TGS fit visualization',
                        'type': 'toggle',
                        'on_text': 'Show',
                        'default': True
                    },
                    {
                        'key': 'plot.settings.num_points',
                        'label': 'Plot points',  # Changed from "Plot Points"
                        'tooltip': 'Number of points to plot (leave empty for all)',
                        'type': 'number',
                        'value_type': int,
                        'default': None
                    }
                ]
            },
            {
                'column': right_column,
                'title': "Current Settings",
                'params': [
                    {
                        'key': 'current_baseline_enabled',
                        'label': 'Baseline enabled',
                        'tooltip': 'Current baseline correction status (read-only)',
                        'type': 'text',
                        'width': 20,
                        'default': 'Disabled',
                        'readonly': True
                    },
                    {
                        'key': 'current_baseline_pos',
                        'label': 'Baseline POS',
                        'tooltip': 'Current positive baseline file (read-only)',
                        'type': 'text',
                        'width': 45,
                        'default': '(none)',
                        'readonly': True
                    },
                    {
                        'key': 'current_baseline_neg',
                        'label': 'Baseline NEG',
                        'tooltip': 'Current negative baseline file (read-only)',
                        'type': 'text',
                        'width': 45,
                        'default': '(none)',
                        'readonly': True
                    }
                ]
            }
        ]
        
        # Build all sections
        for section in sections:
            self._build_intuitive_config_section(section['column'], section['title'], section['params'])
        
        # Buttons at the bottom
        btn_frame = ttk.Frame(scrollable_frame)
        btn_frame.pack(fill='x', pady=10)

        # Left side buttons (Load, Save As, Factory Reset)
        left_btn_frame = ttk.Frame(btn_frame)
        left_btn_frame.pack(side='left')

        btn_load = self.create_button(left_btn_frame, text="Load config...",  # Changed from "Load Config..."
                            command=lambda: self.load_config_file(editor_window))
        btn_load.pack(side='left', padx=5)
        self.add_tooltip(btn_load, "Load settings from an existing YAML configuration file")

        btn_save_as = self.create_button(left_btn_frame, text="Save config as...",  # Changed from "Save Config As..."
                                command=lambda: self.save_config_as(editor_window))
        btn_save_as.pack(side='left', padx=5)
        self.add_tooltip(btn_save_as, "Save current settings to a new YAML file")

        btn_reset = self.create_button(left_btn_frame, text="Factory reset",  # Changed from "Factory Reset"
                            command=lambda: self.factory_reset_config(editor_window))
        btn_reset.pack(side='left', padx=5)
        self.add_tooltip(btn_reset, "Reset all fitting parameters to factory defaults")

        # Right side buttons (Save, Cancel)
        right_btn_frame = ttk.Frame(btn_frame)
        right_btn_frame.pack(side='right')

        btn_save = self.create_button(right_btn_frame, text="Update & close", command=lambda: self.save_config_from_editor(editor_window))
        btn_save.pack(side='right', padx=5)
        self.add_tooltip(btn_save, "Update all fitting parameter changes and close editor")

        btn_cancel = self.create_button(right_btn_frame, text="Cancel", command=editor_window.destroy)
        btn_cancel.pack(side='right', padx=5)
        self.add_tooltip(btn_cancel, "Discard changes and close editor")

        btn_refresh = self.create_button(left_btn_frame, text="Refresh current settings", 
                        command=lambda: self._refresh_editor_current_settings(editor_window))
        btn_refresh.pack(side='left', padx=5)
        self.add_tooltip(btn_refresh, "Refresh the displayed current settings (baseline status and file paths)")
        
        def on_mousewheel(event):
            try:
                # Check if canvas still exists
                if canvas.winfo_exists():
                    canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            except (tk.TclError, AttributeError):
                pass
        
        # Bind to the specific canvas, not globally
        canvas.bind_all("<MouseWheel>", on_mousewheel)

    
    def _refresh_editor_current_settings(self, editor_window):
        """Refresh the current settings display in the config editor"""
        # Update the current settings labels
        for full_key, widget_data in self.config_widgets.items():
            if full_key.startswith('current_'):
                # Check if it's a readonly field (3 elements) or regular (2 elements)
                if len(widget_data) == 3:
                    var, typ, readonly = widget_data
                else:
                    var, typ = widget_data
                    readonly = False
                
                if readonly:
                    if full_key == 'current_baseline_enabled':
                        var.set("Enabled" if self.baseline_var.get() else "Disabled")
                    elif full_key == 'current_baseline_pos':
                        var.set(self.baseline_pos_file if self.baseline_pos_file else "(none)")
                    elif full_key == 'current_baseline_neg':
                        var.set(self.baseline_neg_file if self.baseline_neg_file else "(none)")
        
        self.log_message("Current settings refreshed")

    def save_config_as(self, editor_window=None):
        """Save current configuration (from editor widgets) to a user-selected YAML file"""
        # First collect the current widget values
        temp_config = self._collect_config_from_widgets()
        
        file_path = filedialog.asksaveasfilename(
            defaultextension=".yaml",
            filetypes=[
                ("YAML files", "*.yaml *.yml"),
                ("All files", "*.*")
            ],
            title="Save Configuration As"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'w') as f:
                yaml.dump(temp_config, f, sort_keys=False)
            self.log_message(f"Configuration saved to: {file_path}")
        except Exception as e:
            self.log_message(f"Error saving configuration: {str(e)}", logging.ERROR)
            if editor_window:
                self.log_message(f"Failed to save configuration:\n{str(e)}", logging.WARNING)

    def _save_fit_settings(self, log_path, data_dir):
        """
        Save all fit settings used in the batch processing to a file.
        Creates a file with _fit_settings suffix containing configuration snapshot.
        """
        try:
            # Create the fit settings filename based on the log path
            settings_path = Path(log_path)
            settings_dir = settings_path.parent
            settings_base = settings_path.stem
            
            # Remove any _postprocessing suffix if present
            if settings_base.endswith('_postprocessing'):
                settings_base = settings_base.replace('_postprocessing', '')
            
            settings_filename = settings_dir / f"{settings_base}_fit_settings.yaml"
            
            # Collect current settings
            settings_data = {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'study_name': self.study_name_var.get().strip(),
                'operator': self.operator_var.get().strip(),
                'data_directory': str(data_dir),
                'grating_spacing_um': float(self.grating_edit.get().strip()),
                'files_processed': [],
                'config_snapshot': self._clean_numpy_types(self.config.copy()),
                'batch_info': {
                    'total_files': len(self.pos_files),
                    'successful_fits': len(self.file_to_fit_params),
                    'failed_fits': len(self.pos_files) - len(self.file_to_fit_params)
                }
            }
            
            # Add list of processed files
            for pos_file, neg_file in zip(self.pos_files, self.neg_files):
                file_id = Path(pos_file).stem
                status = '✓' if file_id in self.processed_files else '✗'
                settings_data['files_processed'].append({
                    'run_name': file_id,
                    'pos_file': pos_file,
                    'neg_file': neg_file,
                    'status': status,
                    'has_fit_data': file_id in self.file_to_fit_params
                })
            
            # Add baseline info if enabled
            if self.baseline_var.get() and self.baseline_pos_file and self.baseline_neg_file:
                settings_data['baseline'] = {
                    'enabled': True,
                    'pos_file': self.baseline_pos_file,
                    'neg_file': self.baseline_neg_file
                }
            else:
                settings_data['baseline'] = {'enabled': False}
            
            # Add calibration info if available
            if self.calib_pos_file and self.calib_neg_file:
                settings_data['calibration'] = {
                    'pos_file': self.calib_pos_file,
                    'neg_file': self.calib_neg_file
                }
            
            # Write to YAML file
            with open(settings_filename, 'w') as f:
                yaml.dump(settings_data, f, sort_keys=False, default_flow_style=False, indent=2)
            
            self.log_message(f"Fit settings saved to: {settings_filename}")
            return str(settings_filename)
            
        except Exception as e:
            self.log_message(f"Error saving fit settings: {str(e)}", logging.ERROR)
            return None

    def save_preferences(self):
        """Save user preferences to a JSON file"""
        preferences = {
            "scope_type": self.scope_type_var.get().strip(),
            "scope_address": self.scope_address_var.get().strip(),
            "data_directory": self.data_dir_var.get().strip(),
            "study_name": self.study_name_var.get().strip(),
            "run_name": self.run_name_var.get().strip(),
            "operator": self.operator_var.get().strip(),
            "acq_grating": self.acq_grating_var.get().strip(),
            "num_traces": self.num_traces_var.get().strip(),
            "trigger_rate": self.trigger_rate_var.get().strip(),
            "continuous_acq": self.continuous_acq_var.get(),
            "autofit": self.autofit_var.get(),
        }
        
        # Determine preferences file location
        if getattr(sys, 'frozen', False):
            prefs_path = Path(os.path.dirname(sys.executable)) / "preferences.json"
        else:
            prefs_path = Path("preferences.json")
        
        try:
            with open(prefs_path, 'w') as f:
                json.dump(preferences, f, indent=2)
            # Don't log this - it's too noisy
        except Exception as e:
            self.log_message(f"Failed to save preferences: {str(e)}", logging.WARNING)

    def load_preferences(self):
        """Load user preferences from JSON file"""
        if getattr(sys, 'frozen', False):
            prefs_path = Path(os.path.dirname(sys.executable)) / "preferences.json"
        else:
            prefs_path = Path("preferences.json")
        
        if not prefs_path.exists():
            return
        
        try:
            with open(prefs_path, 'r') as f:
                preferences = json.load(f)
            
            # Apply loaded preferences
            if "scope_type" in preferences and preferences["scope_type"]:
                self.scope_type_var.set(preferences["scope_type"])
                self.current_scope_type = preferences["scope_type"]
            
            if "scope_address" in preferences and preferences["scope_address"]:
                self.scope_address_var.set(preferences["scope_address"])
            if "data_directory" in preferences and preferences["data_directory"]:
                self.data_dir_var.set(preferences["data_directory"])
            if "study_name" in preferences and preferences["study_name"]:
                self.study_name_var.set(preferences["study_name"])
            if "run_name" in preferences and preferences["run_name"]:
                self.run_name_var.set(preferences["run_name"])
            if "operator" in preferences and preferences["operator"]:
                self.operator_var.set(preferences["operator"])
            if "acq_grating" in preferences and preferences["acq_grating"]:
                self.acq_grating_var.set(preferences["acq_grating"])
            if "num_traces" in preferences and preferences["num_traces"]:
                self.num_traces_var.set(preferences["num_traces"])
            if "trigger_rate" in preferences and preferences["trigger_rate"]:
                self.trigger_rate_var.set(preferences["trigger_rate"])
            if "continuous_acq" in preferences:
                self.continuous_acq_var.set(preferences["continuous_acq"])
            if "autofit" in preferences:
                self.autofit_var.set(preferences["autofit"])
            
            self.log_message("Loaded saved preferences")
        except Exception as e:
            self.log_message(f"Failed to load preferences: {str(e)}", logging.WARNING)

    def _collect_config_from_widgets(self):
        """Collect current configuration from editor widgets"""
        temp_config = {}
        # Start with a deep copy of current config
        import copy
        temp_config = copy.deepcopy(self.config)
        
        for full_key, widget_data in self.config_widgets.items():
            parts = full_key.split('.')
            target = temp_config
            
            # Navigate to the correct nested dictionary
            for part in parts[:-1]:
                if part not in target:
                    target[part] = {}
                target = target[part]
            
            last = parts[-1]
            
            # Handle different widget types
            if len(widget_data) == 2:  # Regular widget (var, typ)
                var, typ = widget_data
                value = var.get()
                
                if typ == bool:
                    target[last] = bool(value)
                elif typ == int:
                    try:
                        target[last] = int(float(value))
                    except (ValueError, TypeError):
                        target[last] = 0
                elif typ == float:
                    try:
                        target[last] = float(value)
                    except (ValueError, TypeError):
                        target[last] = 0.0
                else:
                    target[last] = value
            elif len(widget_data) == 3:  # Range widget (lower_var, upper_var, typ)
                lower_var, upper_var, typ = widget_data
                try:
                    if typ == int:
                        target[last] = [int(float(lower_var.get())), int(float(upper_var.get()))]
                    else:
                        target[last] = [float(lower_var.get()), float(upper_var.get())]
                except (ValueError, TypeError):
                    target[last] = [0, 0]
        
        return temp_config

    def _build_intuitive_config_section(self, parent, section_title, params_config):
        """Build a more intuitive configuration section with better widgets"""
        frame = ttk.LabelFrame(parent, text=section_title, padding=(8, 5))
        frame.pack(fill='x', pady=(0, 8))
        
        for param in params_config:
            row = ttk.Frame(frame, style='Panel.TFrame')
            row.pack(fill='x', pady=3)
            
            # Parameter label with tooltip
            label = ttk.Label(row, text=param['label'], width=24, anchor='e')
            label.pack(side='left', padx=(0, 8))
            self.add_tooltip(label, param['tooltip'])
            
            full_key = param['key']
            current_value = self._get_nested_config_value(full_key)
            
            if param['type'] == 'toggle':
                # Boolean toggle using Checkbutton - handle None or empty values
                if current_value is None:
                    bool_value = param.get('default', False)
                elif isinstance(current_value, bool):
                    bool_value = current_value
                else:
                    # Convert to boolean if it's a string or other type
                    bool_value = bool(current_value) if current_value else False
                
                var = tk.BooleanVar(value=bool_value)
                widget = ttk.Checkbutton(row, text=param.get('on_text', 'Enabled'), variable=var)
                widget.pack(side='left', padx=(0, 5))
                self.config_widgets[full_key] = (var, bool)
                self.add_tooltip(widget, param['tooltip'])
                
            elif param['type'] == 'select':
                # Dropdown selection with dark theme
                if current_value is None:
                    default_value = param.get('default', param['options'][0])
                else:
                    default_value = str(current_value)
                
                var = tk.StringVar(value=default_value)
                widget = ttk.Combobox(row, textvariable=var, values=param['options'], state='readonly', width=20, style='Panel.TCombobox')
                widget.pack(side='left', padx=(0, 5))
                # Store the appropriate type
                target_type = param.get('value_type', str)
                self.config_widgets[full_key] = (var, target_type)
                self.add_tooltip(widget, param['tooltip'])
                
            elif param['type'] == 'range':
                # Two entry boxes for lower and upper bounds
                range_frame = ttk.Frame(row, style='Panel.TFrame')
                range_frame.pack(side='left', padx=(0, 5))
                
                # Lower bound
                lower_label = ttk.Label(range_frame, text="min:", font=('Arial', 8), foreground=self.fg_color)
                lower_label.pack(side='left', padx=(2, 2))
                
                lower_var = tk.StringVar()
                if current_value and isinstance(current_value, list) and len(current_value) >= 1:
                    lower_var.set(str(current_value[0]))
                elif param.get('default'):
                    lower_var.set(str(param['default'][0]))
                else:
                    lower_var.set("0")
                
                lower_entry = ttk.Entry(range_frame, textvariable=lower_var, width=10, style='Panel.TEntry')
                lower_entry.pack(side='left', padx=(0, 5))
                
                # Upper bound
                upper_label = ttk.Label(range_frame, text="max:", font=('Arial', 8), foreground=self.fg_color)
                upper_label.pack(side='left', padx=(2, 2))
                
                upper_var = tk.StringVar()
                if current_value and isinstance(current_value, list) and len(current_value) >= 2:
                    upper_var.set(str(current_value[1]))
                elif param.get('default'):
                    upper_var.set(str(param['default'][1]))
                else:
                    upper_var.set("100")
                
                upper_entry = ttk.Entry(range_frame, textvariable=upper_var, width=10, style='Panel.TEntry')
                upper_entry.pack(side='left')
                
                # Store both variables with a special handler
                self.config_widgets[full_key] = (lower_var, upper_var, param.get('value_type', float))
                self.add_tooltip(lower_entry, param['tooltip'])
                self.add_tooltip(upper_entry, param['tooltip'])
                
            elif param['type'] == 'number':
                # Numeric entry with optional bounds
                if current_value is None:
                    default_value = str(param.get('default', 0))
                else:
                    default_value = str(current_value)
                
                var = tk.StringVar(value=default_value)
                widget = ttk.Entry(row, textvariable=var, width=15, style='Panel.TEntry')
                widget.pack(side='left', padx=(0, 5))
                
                # Add validation for numbers
                value_type = param.get('value_type', float)
                
                def validate_number(P, vt=value_type):
                    if P == "" or P == "-" or P == ".":
                        return True
                    try:
                        if vt == int:
                            int(P)
                        else:
                            float(P)
                        return True
                    except:
                        return False
                
                validate_cmd = row.register(validate_number)
                widget.config(validate='key', validatecommand=(validate_cmd, '%P'))
                
                self.config_widgets[full_key] = (var, value_type)
                self.add_tooltip(widget, param['tooltip'])
                
            elif param['type'] == 'text':
                # Text entry (for paths, names, etc.)
                if current_value is None:
                    default_value = param.get('default', '')
                else:
                    default_value = str(current_value)
                
                # Check if this is a readonly field
                is_readonly = param.get('readonly', False)
                
                var = tk.StringVar(value=default_value)
                
                if is_readonly:
                    # For readonly fields, use a label with proper styling
                    widget = ttk.Label(row, text=default_value, foreground=self.fg_color, 
                                    background=self.panel_bg, wraplength=400, anchor='w')
                    widget.pack(side='left', fill='x', expand=True, padx=(0, 5))
                    # Store the variable and flag for refresh purposes
                    self.config_widgets[full_key] = (var, str, True)  # Third element indicates readonly
                else:
                    widget = ttk.Entry(row, textvariable=var, width=param.get('width', 30), style='Panel.TEntry')
                    widget.pack(side='left', fill='x', expand=True, padx=(0, 5))
                    self.config_widgets[full_key] = (var, str)
                
                self.add_tooltip(widget, param['tooltip'])
    
    def _get_nested_config_value(self, key):
        """Get a nested config value by dot-separated key, with special handling for current file paths"""
        # Handle special keys for current file settings
        if key == 'current_baseline_enabled':
            return "Enabled" if self.baseline_var.get() else "Disabled"
        elif key == 'current_baseline_pos':
            return self.baseline_pos_file if self.baseline_pos_file else "(none)"
        elif key == 'current_baseline_neg':
            return self.baseline_neg_file if self.baseline_neg_file else "(none)"
        
        parts = key.split('.')
        target = self.config
        for part in parts:
            if isinstance(target, dict) and part in target:
                target = target[part]
            else:
                return None
        return target
    
    def save_config_from_editor(self, editor_window):
        """Save config from editor and update main GUI"""
        for full_key, widget_data in self.config_widgets.items():
            # Skip readonly fields (current_*)
            if full_key.startswith('current_'):
                continue
                
            parts = full_key.split('.')
            target = self.config
            
            # Navigate to the correct nested dictionary
            for part in parts[:-1]:
                if part not in target:
                    target[part] = {}
                if isinstance(target[part], dict):
                    target = target[part]
                else:
                    self.log_message(f"Warning: Cannot set {full_key} - path structure issue", logging.WARNING)
                    break
            else:
                last = parts[-1]
                
                # Handle different widget types based on tuple length
                if len(widget_data) == 2:  # Regular widget (var, typ)
                    var, typ = widget_data
                    value = var.get()
                    
                    if typ == bool:
                        target[last] = bool(value)
                    elif typ == int:
                        try:
                            target[last] = int(float(value))
                        except (ValueError, TypeError):
                            target[last] = 0
                    elif typ == float:
                        try:
                            target[last] = float(value)
                        except (ValueError, TypeError):
                            target[last] = 0.0
                    elif typ == list:
                        if value and isinstance(value, str):
                            try:
                                target[last] = [float(v.strip()) if '.' in v else int(v.strip()) 
                                            for v in value.split(',') if v.strip()]
                            except ValueError:
                                target[last] = []
                        else:
                            target[last] = []
                    else:
                        target[last] = value
                        
                elif len(widget_data) == 3:  # Range widget (lower_var, upper_var, typ)
                    lower_var, upper_var, typ = widget_data
                    try:
                        if typ == int:
                            target[last] = [int(float(lower_var.get())), int(float(upper_var.get()))]
                        else:
                            target[last] = [float(lower_var.get()), float(upper_var.get())]
                    except (ValueError, TypeError):
                        target[last] = [0, 0]
        
        # Update main GUI parameters from saved config
        self.start_point_var.set(self.config['signal_process']['null_point'])
        self.two_saw_var.set(self.config['lorentzian']['bimodal_fit'])
        self.baseline_var.set(self.config['signal_process']['baseline_correction']['enabled'])
        
        if 'tgs' in self.config and 'grating_spacing' in self.config['tgs']:
            self.grating_edit.delete(0, tk.END)
            self.grating_edit.insert(0, f"{self.config['tgs']['grating_spacing']:.6f}")
        
        self.save_config()
        self.log_message("Configuration updated from editor")
        editor_window.destroy()
    
    def toggle_baseline_ui(self):
        """Enable/disable baseline UI elements"""
        state = 'normal' if self.baseline_var.get() else 'normal'
        self.baseline_button.config(state=state)
    
    def select_calib_files(self):
        """Select calibration files"""
        files = filedialog.askopenfilenames(
            title="Select Calibration files",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not files:
            return
        
        pos_file, neg_file = self.match_files(files)
        if pos_file and neg_file:
            self.calib_pos_file = pos_file
            self.calib_neg_file = neg_file
            pos_name = Path(pos_file).stem
            neg_name = Path(neg_file).stem
            self.calib_file_label.config(text=f"P: {pos_name} | N: {neg_name}", foreground=self.fg_color)
        else:
            self.log_message("No matching POS/NEG pair found.", logging.WARNING)
    
    def select_baseline_files(self):
        """Select baseline files"""
        files = filedialog.askopenfilenames(
            title="Select Baseline files",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not files:
            return
        
        pos_file, neg_file = self.match_files(files)
        if pos_file and neg_file:
            self.baseline_pos_file = pos_file
            self.baseline_neg_file = neg_file
            pos_name = Path(pos_file).stem
            neg_name = Path(neg_file).stem
            self.baseline_file_label.config(text=f"P: {pos_name} | N: {neg_name}", foreground=self.fg_color)
        else:
            self.log_message("No matching baseline POS/NEG pair found.", logging.WARNING)
            messagebox.showerror("Error", )
    
    def match_files(self, file_list):
        """Match POS and NEG files from a list"""
        pos_file = None
        neg_file = None
        
        for f in file_list:
            name = Path(f).stem.upper()
            if 'POS' in name:
                pos_file = f
            elif 'NEG' in name:
                neg_file = f
        
        if pos_file and neg_file:
            pos_base = Path(pos_file).stem.upper().replace('POS', '')
            neg_base = Path(neg_file).stem.upper().replace('NEG', '')
            if pos_base != neg_base:
                return None, None
        
        return pos_file, neg_file
    
    def run_calibration(self):
        """Run calibration to determine grating spacing"""
        if not self.calib_pos_file or not self.calib_neg_file:
            self.log_message("Please select calibration files.", logging.WARNING)
            return
        
        self.log_message("Starting calibration...")
        
        def calibration_thread():
            try:
                temp_config = {
                    'signal_process': {
                        'heterodyne': 'di-homodyne',
                        'null_point': int(self.start_point_var.get()),
                        'initial_samples': 50,
                        'baseline_correction': {'enabled': False}
                    },
                    'fft': self.config['fft'],
                    'lorentzian': self.config['lorentzian'].copy(),
                    'plot': {'signal_process': False, 'fft_lorentzian': False, 'tgs': False}
                }
                temp_config['lorentzian']['bimodal_fit'] = self.two_saw_var.get()
                
                signal, _, _, _ = process_signal(
                    temp_config, None, 0, self.calib_pos_file, self.calib_neg_file, 
                    grating_spacing=1.0, **temp_config['signal_process']
                )
                
                time = signal[:, 0]
                amp = signal[:, 1]
                dt = time[1] - time[0]
                derivative = np.gradient(amp, dt)
                saw_signal = np.column_stack((time[:-1], derivative[:-1]))
                
                fft_signal = fft(saw_signal, **temp_config['fft'])
                
                # lorentzian_fit now returns 10 values, we only need the first 2 (f, f_err)
                # Unpack all values but only use what we need
                result = lorentzian_fit(
                    temp_config, None, 0, fft_signal, **temp_config['lorentzian']
                )
                
                # Extract f and f_err from the 10-value tuple
                f = result[0]
                f_err = result[1]
                # The rest: fwhm, tau, snr, frequency_bounds, fit_function, popt, fft_segment, fft_full, lorentzian_curve = result[2:]
                
                if isinstance(f, np.ndarray):
                    f = f[0]
                grating_spacing_um = (self.sound_speed / f) * 1e6
                self.calibrated_spacing = grating_spacing_um
                
                self.root.after(0, lambda: self.update_calibration_result(f, grating_spacing_um))
                self.log_message(f"Calibration: f = {f/1e6:.3f} MHz, grating = {grating_spacing_um:.4f} µm")
                
                if self.close_plots_var.get():
                    import matplotlib.pyplot as plt
                    plt.close('all')
                    
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Calibration Error", str(e)))
                self.log_message(f"Calibration error: {str(e)}", logging.ERROR)
        
        thread = threading.Thread(target=calibration_thread)
        thread.daemon = True
        thread.start()
    
    def update_calibration_result(self, frequency, grating_spacing):
        """Update UI with calibration results"""
        self.grating_edit.delete(0, tk.END)
        self.grating_edit.insert(0, f"{grating_spacing:.6f}")
    
    def browse_log_file(self):
        """Browse for log file location"""
        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if file_path:
            self.log_file_var.set(file_path)
    
    def add_batch_files(self):
        """Add files to batch queue"""
        files = filedialog.askopenfilenames(
            title="Select TGS files for batch processing",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not files:
            return
        
        pos_neg_pairs = {}
        for f in files:
            name = Path(f).stem
            base = name.upper().replace('POS', '').replace('NEG', '')
            if 'POS' in name.upper():
                pos_neg_pairs[base] = {'pos': f, 'neg': pos_neg_pairs.get(base, {}).get('neg')}
            elif 'NEG' in name.upper():
                pos_neg_pairs[base] = {'neg': f, 'pos': pos_neg_pairs.get(base, {}).get('pos')}
        
        added = 0
        for base, pair in pos_neg_pairs.items():
            if pair.get('pos') and pair.get('neg'):
                self.pos_files.append(pair['pos'])
                self.neg_files.append(pair['neg'])
                added += 1
        
        # Update the listbox with processed status
        self._update_batch_listbox_processed_status()
        
        self.log_message(f"Added {added} file pairs to queue")
        self.root.after(0, self.update_summary_plot)
        if added == 0:
            self.log_message("No matching POS/NEG pairs found. Please select both POS and NEG files for each run.", logging.WARNING)
    
    def clear_queue(self):
        """Clear the batch queue"""
        if not self.pos_files:
            self.log_message("Queue is already empty", logging.WARNING)
            return
        
        if messagebox.askyesno("Confirm Clear", f"Are you sure you want to clear {len(self.pos_files)} file pair(s) from the queue?"):
            self.pos_files = []
            self.neg_files = []
            self.processed_files.clear()  # Clear processed status
            self.batch_listbox.delete(0, tk.END)
            self.file_to_plot_path.clear()
            self.file_to_fit_params.clear()
            if hasattr(self, 'file_to_fit_plot_data'):
                self.file_to_fit_plot_data.clear()
            self.clear_results_table()
            self.root.after(0, self.update_summary_plot)
            self.log_message("Queue cleared")
    
    def run_batch(self):
        """Run batch processing"""
        # Check if batch is already running
        if hasattr(self, 'running_job') and self.running_job and self.running_job.is_alive():
            self.log_message("Batch processing is already running. Please wait or press Stop first.", logging.WARNING)
            return
        
        print(f"[BATCH] Starting batch with {len(self.pos_files)} files at {datetime.now()}")

        grating_text = self.grating_edit.get().strip()
        if not grating_text or float(grating_text) <= 0:
            self.log_message("Please run calibration or enter a valid grating spacing.", logging.WARNING)
            return
        
        # Check baseline files if baseline is enabled
        if self.baseline_var.get():
            if not self.baseline_pos_file or not self.baseline_neg_file:
                self.log_message("Baseline correction is enabled but no baseline files selected.\n\nPlease either:\n1. Select baseline files using the 'Select baseline' button, or\n2. Disable 'Use baseline' checkbox.", logging.WARNING)
                return

        # Check if files exist
        if not self.pos_files:
            self.log_message("No files in queue. Please add files first.", logging.WARNING)
            return
        
        null_point = self.start_point_var.get()
        if null_point < 1 or null_point > 4:
            self.log_message(f"Warning: null_point={null_point} is invalid, setting to 2")
            null_point = 2
            self.start_point_var.set(2)

        self.set_ui_state('batch_processing')

        self.config['signal_process']['null_point'] = int(null_point)
        self.config['lorentzian']['bimodal_fit'] = bool(self.two_saw_var.get())
        self.config['signal_process']['baseline_correction']['enabled'] = bool(self.baseline_var.get())
        self.config['tgs']['grating_spacing'] = float(grating_text)
        
        if 'dc_filter_range' in self.config['lorentzian']:
            dc_range = self.config['lorentzian']['dc_filter_range']
            if isinstance(dc_range, list) and len(dc_range) == 2:
                self.config['lorentzian']['dc_filter_range'] = [int(dc_range[0]), int(dc_range[1])]
        
        if self.baseline_var.get() and self.baseline_pos_file and self.baseline_neg_file:
            self.config['signal_process']['baseline_correction']['pos'] = self.baseline_pos_file
            self.config['signal_process']['baseline_correction']['neg'] = self.baseline_neg_file
        
        show_plots = not self.close_plots_var.get()
        self.config['plot']['signal_process'] = show_plots
        self.config['plot']['fft_lorentzian'] = show_plots
        self.config['plot']['tgs'] = show_plots
        
        if self.config['plot']['settings']['num_points'] is None or self.config['plot']['settings']['num_points'] == 'None':
            self.config['plot']['settings']['num_points'] = 10000
        
        data_dir = Path(self.pos_files[0]).parent
        self.config['path'] = str(data_dir)
        
        log_path = self.log_file_var.get().strip()
        if not log_path:
            first_file = Path(self.pos_files[0])
            log_path = first_file.parent / f"{first_file.stem.split('-POS')[0]}_postprocessing.txt"
        else:
            log_path = Path(log_path)
            if log_path.suffix == '':
                log_path = log_path.with_suffix('.txt')
            log_path = str(log_path)
        
        self.current_results_log_path = log_path
        if not self.log_file_var.get().strip():
            self.log_file_var.set(str(log_path))
        
        self.save_config()
        self.log_message(f"Starting batch processing of {len(self.pos_files)} file pairs...")
        self.log_message(f"Results will be saved to: {log_path}")
        
        # Configure progress bar
        self.progress.configure(mode='determinate', maximum=len(self.pos_files), value=0)
        self.progress['value'] = 0  # Explicitly set value
        self.status_var.set("Starting batch... (0/{})".format(len(self.pos_files)))
        
        self.stop_batch = False
        
        self.run_button.config(state='disabled')
        self.stop_button.config(state='normal')
        
        self.file_to_plot_path.clear()
        self.file_to_fit_params.clear()
        
        # Initialize time tracking
        self.batch_start_time = None
        self.first_run_duration = None
        self.current_run_start_time = None
        self.status_var.set("Starting batch...")
        
        def batch_thread():
            try:
                self._run_direct_batch(log_path)
                if not self.stop_batch:
                    self.root.after(0, self._batch_finished, True, "Batch processing completed.")
                else:
                    self.root.after(0, self._batch_finished, True, "Batch processing stopped by user.")
            except Exception as e:
                self.root.after(0, self._batch_finished, False, str(e))
        
        thread = threading.Thread(target=batch_thread)
        thread.daemon = True
        self.running_job = thread
        thread.start()
    
    def update_status_with_time(self, current_file_index, total_files):
        """Update the status message with estimated time remaining"""
        import time
        
        # For the first file, just show estimating
        if current_file_index == 1:
            if self.batch_start_time is None:
                self.batch_start_time = time.time()
            self.status_var.set(f"Processing file 1/{total_files}... (estimating...)")
            self.root.update_idletasks()
            return
        
        # Check if batch_start_time is valid
        if self.batch_start_time is None:
            self.status_var.set(f"Processing file {current_file_index}/{total_files}...")
            self.root.update_idletasks()
            return
        
        # Calculate duration so far
        elapsed_time = time.time() - self.batch_start_time
        
        # Calculate average time per file based on completed files
        completed_files = current_file_index - 1
        if completed_files > 0:
            avg_time_per_file = elapsed_time / completed_files
            
            # Calculate remaining files
            remaining_files = total_files - current_file_index + 1
            estimated_remaining = avg_time_per_file * remaining_files
            
            if estimated_remaining > 0:
                if estimated_remaining < 60:
                    time_str = f"{estimated_remaining:.0f} seconds"
                elif estimated_remaining < 3600:
                    minutes = int(estimated_remaining // 60)
                    seconds = int(estimated_remaining % 60)
                    time_str = f"{minutes} min {seconds} sec"
                else:
                    hours = int(estimated_remaining // 3600)
                    minutes = int((estimated_remaining % 3600) // 60)
                    time_str = f"{hours} hr {minutes} min"
                
                self.status_var.set(f"Processing file {current_file_index}/{total_files}... ~{time_str} remaining")
            else:
                self.status_var.set(f"Processing file {current_file_index}/{total_files}...")
        else:
            self.status_var.set(f"Processing file {current_file_index}/{total_files}...")
        
        self.root.update_idletasks()

    def stop_batch_processing(self):
        """Stop the batch processing gracefully"""
        self.log_message("Stop button pressed!")
        
        if hasattr(self, 'stop_batch') and self.stop_batch:
            self.log_message("Already stopping...")
            return
            
        if hasattr(self, 'running_job') and self.running_job and self.running_job.is_alive():
            self.log_message("Stopping batch processing...")
            self.stop_batch = True
            self.status_var.set("Stopping... (finishing current file)")
        else:
            self.log_message("No batch processing in progress", logging.WARNING)

    def _run_direct_batch(self, log_path):
        """Run batch directly using tgs_fit with memory-efficient plot handling"""
        
        # Import and configure matplotlib for batch mode
        import matplotlib
        matplotlib.use('Agg')  # Force non-GUI backend for batch processing
        import matplotlib.pyplot as plt
        from src.analysis.tgs import tgs_fit
        import copy
        import gc
        import time
        
        # Clear any existing plots and force garbage collection
        plt.close('all')
        gc.collect()
        
        # Clear the preview
        self.root.after(0, self.clear_preview)
        
        # Clear the plot data cache at start of batch
        if hasattr(self, 'file_to_fit_plot_data'):
            self.file_to_fit_plot_data.clear()
            gc.collect()
        
        results = []
        total = len(self.pos_files)
        
        data_dir = Path(self.pos_files[0]).parent
        
        from src.core.path import Paths
        paths = Paths(
            data_dir=data_dir,
            figure_dir=data_dir / 'figures',
            fit_dir=data_dir / 'fit',
            fit_path=data_dir / 'fit' / 'fit.csv',
            signal_path=data_dir / 'fit' / 'signal.json',
        )
        paths.figure_dir.mkdir(parents=True, exist_ok=True)
        paths.fit_dir.mkdir(parents=True, exist_ok=True)
        
        show_plots = not self.close_plots_var.get()
        self.config['plot']['signal_process'] = show_plots
        self.config['plot']['fft_lorentzian'] = show_plots
        self.config['plot']['tgs'] = show_plots
        
        if self.config['plot']['settings']['num_points'] is None or self.config['plot']['settings']['num_points'] == 'None':
            self.config['plot']['settings']['num_points'] = 10000
        
        # Use the calibration grating value
        grating_spacing_val = float(self.grating_edit.get().strip()) if self.grating_edit.get().strip() else self.config.get('tgs', {}).get('grating_spacing', 3.5276)
        
        # Track the first successful fit for display
        first_fit_displayed = False
        
        for i, (pos_file, neg_file) in enumerate(zip(self.pos_files, self.neg_files), 1):
            
            # Check stop flag at the beginning of each iteration
            if self.stop_batch:
                self.log_message("Batch processing stopped by user.")
                break
            
            file_id = Path(pos_file).stem
            
            # Simple progress message
            self.log_message(f"[{i}/{total}] {file_id}...")
            print(f"[BATCH] Processing file {i}/{total}: {file_id}")
            
            # Update progress bar
            def update_progress(val, tot):
                self.progress.configure(value=val)
                self.root.update_idletasks()
            
            self.root.after(0, update_progress, i, total)
            
            # Update status with time estimate
            self.root.after(0, lambda: self.update_status_with_time(i, total))
            
            # Process GUI events before starting the fit
            try:
                self.root.update_idletasks()
                self.root.update()
            except:
                pass
            
            # Check stop flag again before starting the fit
            if self.stop_batch:
                self.log_message("Batch processing stopped by user.")
                break
            
            try:
                file_config = copy.deepcopy(self.config)
                
                file_config['signal_process']['null_point'] = int(file_config['signal_process']['null_point'])
                file_config['signal_process']['initial_samples'] = int(file_config['signal_process']['initial_samples'])
                file_config['lorentzian']['dc_filter_range'] = [
                    int(file_config['lorentzian']['dc_filter_range'][0]),
                    int(file_config['lorentzian']['dc_filter_range'][1])
                ]
                
                if file_config['plot']['settings']['num_points'] is None or file_config['plot']['settings']['num_points'] == 'None':
                    file_config['plot']['settings']['num_points'] = 10000
                else:
                    file_config['plot']['settings']['num_points'] = int(file_config['plot']['settings']['num_points'])
                
                # Run tgs_fit in a separate thread with timeout
                fit_result = [None]
                fit_exception = [None]
                
                def run_fit():
                    try:
                        result = tgs_fit(
                            file_config, paths, i, str(pos_file), str(neg_file), 
                            grating_spacing=grating_spacing_val,
                            signal_proportion=float(file_config['tgs']['signal_proportion']),
                            maxfev=int(file_config['tgs']['maxfev'])
                        )
                        fit_result[0] = result
                    except Exception as e:
                        fit_exception[0] = e
                
                fit_thread = threading.Thread(target=run_fit)
                fit_thread.daemon = True
                fit_thread.start()
                
                # Wait for fit to complete with periodic stop checks
                fit_timeout = 300  # 5 minutes timeout per file
                start_wait = time.time()
                while fit_thread.is_alive() and (time.time() - start_wait) < fit_timeout:
                    if self.stop_batch:
                        self.log_message(f"Stopping fit for {file_id} due to stop request")
                        break
                    time.sleep(0.5)
                    try:
                        self.root.update_idletasks()
                    except:
                        pass
                
                if self.stop_batch:
                    self.log_message(f"Batch processing stopped during fit of {file_id}")
                    break
                
                if fit_exception[0] is not None:
                    raise fit_exception[0]
                
                if fit_result[0] is None:
                    raise Exception(f"Fit for {file_id} did not complete")
                
                file_id = Path(pos_file).stem
                self.processed_files.add(file_id)
                self._update_batch_listbox_processed_status()
                
                # Unpack the result
                (start_idx, start_time, grating_spacing, 
                A, A_err, B, B_err, C, C_err, 
                alpha, alpha_err, beta, beta_err, 
                theta, theta_err, tau, tau_err, 
                f, f_err, signal, fft_full, lorentzian_curve) = fit_result[0]

                plt.close('all')

                date_time_str = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
                
                # Store fit parameters ONLY - no plot data
                fit_params = {
                    'A': float(A) if A is not None else None,
                    'A_err': float(A_err) if A_err is not None else None,
                    'B': float(B) if B is not None else None,
                    'B_err': float(B_err) if B_err is not None else None,
                    'C': float(C) if C is not None else None,
                    'C_err': float(C_err) if C_err is not None else None,
                    'alpha': float(alpha) if alpha is not None else None,
                    'alpha_err': float(alpha_err) if alpha_err is not None else None,
                    'beta': float(beta) if beta is not None else None,
                    'beta_err': float(beta_err) if beta_err is not None else None,
                    'theta': float(theta) if theta is not None else None,
                    'theta_err': float(theta_err) if theta_err is not None else None,
                    'tau': float(tau) if tau is not None else None,
                    'tau_err': float(tau_err) if tau_err is not None else None,
                    'f': float(f) if f is not None else None,
                    'f_err': float(f_err) if f_err is not None else None,
                }

                # Store ONLY fit parameters - NO plot data
                self.file_to_fit_params[file_id] = fit_params
                
                # Update the summary plot
                self.root.after(0, self.update_summary_plot)
                
                # Generate plot data for preview
                fit_data_for_plot = {
                    'title': file_id,
                    'time_raw': signal[:, 0],
                    'signal_raw': signal[:, 1],
                }

                # Generate time points for fit
                time_fit = np.linspace(signal[start_idx, 0], signal[-1, 0], 1000)
                from src.analysis.functions import tgs_function
                functional_func, thermal_func = tgs_function(start_time, grating_spacing_val)

                fit_data_for_plot['time_fit'] = time_fit
                fit_data_for_plot['fit_signal'] = functional_func(time_fit, A, B, C, alpha, beta, theta, tau, f)
                fit_data_for_plot['thermal_signal'] = thermal_func(time_fit, A, B, C, alpha, beta, theta, tau, f)

                # Store FFT data
                if fft_full is not None and len(fft_full) > 0:
                    fit_data_for_plot['fft_freq'] = fft_full[:, 0]
                    fit_data_for_plot['fft_amp'] = fft_full[:, 1]
                else:
                    fit_data_for_plot['fft_freq'] = None
                    fit_data_for_plot['fft_amp'] = None

                # Store Lorentzian fit curve if available
                if lorentzian_curve is not None and len(lorentzian_curve) > 0:
                    if fft_full is not None and len(fft_full) > 0:
                        fft_freqs_ghz = fft_full[:, 0]
                        fft_amps = fft_full[:, 1]
                        freq_bounds = self.config['lorentzian'].get('frequency_bounds', [0.1, 0.9])
                        lorentzian_freqs_ghz = np.linspace(freq_bounds[0], freq_bounds[1], len(lorentzian_curve))
                        
                        mask = (fft_freqs_ghz >= freq_bounds[0]) & (fft_freqs_ghz <= freq_bounds[1])
                        if np.any(mask):
                            fft_peak_in_range = np.max(fft_amps[mask])
                        else:
                            fft_peak_in_range = np.max(fft_amps)
                        
                        lorentzian_peak_value = np.max(lorentzian_curve)
                        if lorentzian_peak_value > 0 and fft_peak_in_range > 0:
                            scale_factor = fft_peak_in_range / lorentzian_peak_value
                            lorentzian_curve_scaled = lorentzian_curve * scale_factor
                        else:
                            lorentzian_curve_scaled = lorentzian_curve
                        
                        fit_data_for_plot['lorentzian_freq'] = lorentzian_freqs_ghz
                        fit_data_for_plot['lorentzian_fit'] = lorentzian_curve_scaled
                    else:
                        fit_data_for_plot['lorentzian_fit'] = None
                else:
                    fit_data_for_plot['lorentzian_fit'] = None

                # Update the display for every fit (this will update as batch progresses)
                self.root.after(0, lambda fd=fit_data_for_plot: self.create_interactive_plot(fd))
                self.root.after(0, lambda fp=fit_params: self.update_results_table(fp))
                
                # Force canvas updates
                self.root.after(0, self.summary_canvas.draw_idle)
                
                # For results file
                if isinstance(f, np.ndarray):
                    f_val = float(f[0]) if len(f) > 0 else 0.0
                    f_err_val = float(f_err[0]) if len(f_err) > 0 else 0.0
                    tau_val = float(tau[0]) if len(tau) > 0 else 0.0
                    tau_err_val = float(tau_err[0]) if len(tau_err) > 0 else 0.0
                else:
                    f_val = float(f) if f is not None else 0.0
                    f_err_val = float(f_err) if f_err is not None else 0.0
                    tau_val = float(tau) if tau is not None else 0.0
                    tau_err_val = float(tau_err) if tau_err is not None else 0.0
                
                result = {
                    'run_name': file_id,
                    'date_time': date_time_str,
                    'grating_spacing_um': float(grating_spacing),
                    'SAW_freq_Hz': f_val,
                    'SAW_freq_error_Hz': f_err_val,
                    'A_Wm-2': float(A) if A is not None else 0.0,
                    'A_err_Wm-2': float(A_err) if A_err is not None else 0.0,
                    'alpha_m2s-1': float(alpha) if alpha is not None else 0.0,
                    'alpha_err_m2s-1': float(alpha_err) if alpha_err is not None else 0.0,
                    'beta_s0.5': float(beta) if beta is not None else 0.0,
                    'beta_err_s0.5': float(beta_err) if beta_err is not None else 0.0,
                    'B_Wm-2': float(B) if B is not None else 0.0,
                    'B_err_Wm-2': float(B_err) if B_err is not None else 0.0,
                    'theta_rad': float(theta) if theta is not None else 0.0,
                    'theta_err_rad': float(theta_err) if theta_err is not None else 0.0,
                    'tau_s': tau_val,
                    'tau_err_s': tau_err_val,
                    'C_Wm-2': float(C) if C is not None else 0.0,
                    'C_err_Wm-2': float(C_err) if C_err is not None else 0.0,
                }
                
                results.append(result)
                if len(results) % 10 == 0:
                    self.root.after(0, lambda ddir=data_dir: self.cleanup_old_plots(ddir))
                
                # Update status for next file
                self.root.after(0, lambda: self.update_status_with_time(i, total))

                # Store the image path as fallback
                current_data_dir = Path(pos_file).parent
                combined_plot_path = current_data_dir / 'figures' / 'combined' / f'combined-{file_id}.png'
                if combined_plot_path.exists():
                    self.file_to_plot_path[file_id] = str(combined_plot_path)
                
                # Process GUI events after each file
                try:
                    self.root.update_idletasks()
                    self.root.update()
                except:
                    pass
                
                # ===== MEMORY MANAGEMENT =====
                # Clear the signal and FFT data to free memory
                try:
                    del signal
                    del fft_full
                    del lorentzian_curve
                    if 'fit_data_for_plot' in locals():
                        del fit_data_for_plot
                except:
                    pass
                
                # Force garbage collection periodically
                if i % 5 == 0:
                    gc.collect()
                    print(f"[GC] Garbage collection performed after file {i}")
                
                # Clean up plot cache periodically (keep only last 3 plots in memory)
                if i % 3 == 0 and hasattr(self, 'file_to_fit_plot_data'):
                    keys = list(self.file_to_fit_plot_data.keys())
                    if len(keys) > 3:
                        for key in keys[:-3]:
                            if key in self.file_to_fit_plot_data:
                                del self.file_to_fit_plot_data[key]
                        gc.collect()
                        print(f"[MEMORY] Cleaned plot cache, kept {len(self.file_to_fit_plot_data)} plots")
                
                # Log memory usage every 10 files
                #if i % 10 == 0:
                #    self.log_memory_usage(f"After file {i}/{total}")
                #    self.log_object_sizes(f"After file {i}/{total}")

                #if i % 3 == 0 or i % 10 == 0:
                #    self.log_detailed_memory(f"After file {i}/{total}")
                
                # Force close any extra figures that might be hanging around
                import matplotlib.pyplot as plt
                plt.close('all')
                # ===== END MEMORY MANAGEMENT =====
                
            except Exception as e:
                self.log_message(f" FAILED: {str(e)}", logging.ERROR)
                import traceback
                self.log_message(traceback.format_exc(), logging.DEBUG)
                print(f"[ERROR] File {i} failed: {str(e)}")
                print(traceback.format_exc())
                
                # If we hit a memory error, clear caches
                if "MemoryError" in str(e) or "out of memory" in str(e).lower():
                    print("[MEMORY] Memory error detected, clearing caches...")
                    if hasattr(self, 'file_to_fit_plot_data'):
                        self.file_to_fit_plot_data.clear()
                    gc.collect()
                    print("[MEMORY] Caches cleared")
            
            if self.close_plots_var.get():
                plt.close('all')
            
            # Process GUI events after each file
            try:
                self.root.update_idletasks()
                self.root.update()
            except:
                pass
        
        # Save results
        if results:
            self._save_space_delimited(results, log_path)
            self.log_message(f"Results saved to {log_path}")
        else:
            self.log_message("No successful fits to save.", logging.WARNING)
        
        # Store the data_dir for the settings file
        self._last_batch_data_dir = data_dir
        self._last_batch_log_path = log_path
        
        # Final cleanup
        if hasattr(self, 'file_to_fit_plot_data'):
            self.file_to_fit_plot_data.clear()
        gc.collect()
        plt.close('all')
    
    def get_memory_usage(self):
        """Get current memory usage of the process"""
        try:
            import psutil
            import os
            process = psutil.Process(os.getpid())
            mem_info = process.memory_info()
            return {
                'rss': mem_info.rss / 1024 / 1024,  # MB
                'vms': mem_info.vms / 1024 / 1024,  # MB
            }
        except ImportError:
            return None
        except Exception:
            return None

    def log_memory_usage(self, label=""):
        """Log current memory usage to both the log and console"""
        mem = self.get_memory_usage()
        if mem:
            msg = f"[MEMORY] {label}: RSS={mem['rss']:.1f}MB, VMS={mem['vms']:.1f}MB"
        else:
            msg = f"[MEMORY] {label}: psutil not available"
        # Print to console (stdout)
        print(msg)
        # Also log it
        self.log_message(msg, logging.INFO)
        return mem

    def log_object_sizes(self, label=""):
        """Log sizes of major data structures"""
        sizes = {
            'pos_files': len(self.pos_files),
            'file_to_fit_params': len(self.file_to_fit_params),
            'file_to_fit_plot_data': len(self.file_to_fit_plot_data),
            'processed_files': len(self.processed_files),
        }
        # Estimate memory for plot data
        plot_data_size = 0
        for file_id, data in self.file_to_fit_plot_data.items():
            for key, value in data.items():
                if isinstance(value, np.ndarray):
                    plot_data_size += value.nbytes
                elif isinstance(value, list):
                    plot_data_size += len(value) * 8  # rough estimate
        
        msg = f"[OBJECTS] {label}: {sizes}, plot_data_memory={plot_data_size/1024/1024:.1f}MB"
        print(msg)
        self.log_message(msg, logging.INFO)

    def _update_batch_listbox_processed_status(self):
        """Update the batch listbox to show which files have been processed"""
        # Store current selection
        selection = self.batch_listbox.curselection()
        selected_index = selection[0] if selection else None
        
        # Clear and rebuild the listbox
        self.batch_listbox.delete(0, tk.END)
        
        for i, pos_file in enumerate(self.pos_files, 1):
            pos_name = Path(pos_file).stem
            neg_name = Path(self.neg_files[i-1]).stem
            
            file_id = pos_name
            
            # Check if this file has been processed
            if file_id in self.processed_files:
                display_text = f"[{i}] ✓ P: {pos_name} | N: {neg_name}"
            else:
                display_text = f"[{i}]   P: {pos_name} | N: {neg_name}"
            
            self.batch_listbox.insert(tk.END, display_text)
        
        # Restore selection
        if selected_index is not None and selected_index < self.batch_listbox.size():
            self.batch_listbox.selection_set(selected_index)

    def _save_space_delimited(self, results, log_path):
        """Save results as space-delimited file with header"""
        
        if not results:
            return
        
        headers = [
            'run_name', 'date_time', 'grating_spacing_um', 
            'SAW_freq_Hz', 'SAW_freq_error_Hz',
            'A_Wm-2', 'A_err_Wm-2', 'alpha_m2s-1', 'alpha_err_m2s-1',
            'beta_s0.5', 'beta_err_s0.5', 'B_Wm-2', 'B_err_Wm-2',
            'theta_rad', 'theta_err_rad', 'tau_s', 'tau_err_s',
            'C_Wm-2', 'C_err_Wm-2'
        ]
        
        with open(log_path, 'w') as f:
            f.write(' '.join(headers) + '\n')
            
            for result in results:
                row = []
                for header in headers:
                    value = result.get(header, '')
                    if isinstance(value, str):
                        row.append(value)
                    else:
                        if isinstance(value, float):
                            row.append(f"{value:.8e}")
                        else:
                            row.append(str(value))
                f.write(' '.join(row) + '\n')
    
    def clear_preview(self):
        """Clear the preview display"""
        self.preview_ax.clear()
        self.preview_ax.set_facecolor(self.bg_color)
        self.preview_ax.text(0.5, 0.5, 'Batch processing in progress...\nWaiting for first fit',
                            ha='center', va='center', transform=self.preview_ax.transAxes,
                            color=self.fg_color, fontsize=12, fontfamily='Arial')
        self.preview_ax.set_xlim(0, 1)
        self.preview_ax.set_ylim(0, 1)
        self.preview_ax.set_xticks([])
        self.preview_ax.set_yticks([])
        self.preview_canvas.draw()
        self.clear_results_table()

    def _batch_finished(self, success, message):
        """Handle batch completion"""
        self.set_ui_state('idle')
        self.progress.stop()
        self.progress.configure(mode='indeterminate')
        self.progress.configure(value=0)
        
        self.run_button.config(state='normal')
        
        # Refresh the listbox to show updated processed status
        self._update_batch_listbox_processed_status()
        
        # Save fit settings if we have results
        if self.pos_files and len(self.file_to_fit_params) > 0:
            if hasattr(self, '_last_batch_data_dir') and hasattr(self, '_last_batch_log_path'):
                self._save_fit_settings(self._last_batch_log_path, self._last_batch_data_dir)
            else:
                data_dir = Path(self.pos_files[0]).parent
                log_path = self.log_file_var.get().strip()
                if not log_path:
                    log_path = data_dir / f"{Path(self.pos_files[0]).stem.split('-POS')[0]}_postprocessing.txt"
                self._save_fit_settings(log_path, data_dir)
        
        if success:
            self.status_var.set("Completed")
            self.log_message(message)
        else:
            self.status_var.set("Failed")
            self.log_message(message, logging.ERROR)
            self.log_message(f"Batch processing failed:\n{message}", logging.WARNING)
        
        print(f"[BATCH] Finished at {datetime.now()}, success={success}")

        # Reset time tracking
        self.batch_start_time = None
        self.first_run_duration = None
        self.current_run_start_time = None
    
    def clear_log(self):
        """Clear the log text widget"""
        self.log_text.delete(1.0, tk.END)
    
    def on_close(self):
        """Clean up on exit and save preferences"""
        # Set shutdown flag first
        self.shutdown_flag = True
        
        print(f"[SHUTDOWN] Closing application...")

        # Save user preferences before closing
        self.save_config()
        self.save_preferences()
        
        # Force immediate destruction without waiting for background threads
        # This bypasses the thread join delays
        try:
            self.root.destroy()
        except:
            pass

def main():
    """Main entry point"""
    # Create main window directly
    root = tb.Window(themename="darkly")
    root.title("PyTGS v" + TGSApp.VERSION + " - Transient Grating Analyser")
    root.geometry("1700x1000")
    
    # Try to set icon
    try:
        if getattr(sys, 'frozen', False):
            possible_paths = [
                os.path.join(os.path.dirname(sys.executable), 'dihomodyne_beams_icon.ico'),
                os.path.join(sys._MEIPASS, 'dihomodyne_beams_icon.ico'),
                'dihomodyne_beams_icon.ico'
            ]
        else:
            possible_paths = ['dihomodyne_beams_icon.ico']
        
        icon_path = None
        for path in possible_paths:
            if os.path.exists(path):
                icon_path = path
                break
        
        if icon_path:
            root.iconbitmap(icon_path)
            try:
                icon_image = tk.PhotoImage(file=icon_path)
                root.iconphoto(True, icon_image)
                root._icon_image = icon_image
            except:
                pass
    except:
        pass
    
    # Create the application
    app = TGSApp(root)
    
    # Run the main loop
    root.mainloop()

if __name__ == "__main__":
    main()
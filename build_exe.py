#!/usr/bin/env python3
"""
Build script to create standalone executable for PyTGS GUI
Run: python build_exe.py
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path
import re
from datetime import datetime

# ============================================
# CONFIGURATION - EDIT THIS FOR EACH BUILD
# ============================================
VERSION = "1.0.1"  # <-- CHANGE THIS FOR EACH BUILD
# ============================================

def sanitize_version_for_filename(version):
    """Convert version string to a safe filename (replace special chars)"""
    return version.replace('-', '_')

def update_version_in_file(version):
    """Update version number in the GUI file"""
    gui_file = "tgs_gui.py"
    if not os.path.exists(gui_file):
        print(f"Warning: {gui_file} not found, skipping version update")
        return
    
    try:
        with open(gui_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Update the window title to include version
        old_title_pattern = r'self\.root\.title\("PyTGS v[\d\.\-a-zA-Z]+ - Transient Grating Analyser"\)'
        new_title = f'self.root.title("PyTGS v{version} - Transient Grating Analyser")'
        content = re.sub(old_title_pattern, new_title, content)
        
        # Add or update version variable
        if re.search(r'VERSION = ', content):
            content = re.sub(r'VERSION = "[\d\.\-a-zA-Z]+"', f'VERSION = "{version}"', content)
        else:
            version_var_pattern = r'(class TGSApp:.*?)(def __init__)'
            version_insert = r'\1    VERSION = "' + version + r'"\n\n    \2'
            content = re.sub(version_var_pattern, version_insert, content, flags=re.DOTALL)
        
        with open(gui_file, 'w', encoding='utf-8') as f:
            f.write(content)
        
        print(f" Updated version to {version} in {gui_file}")
    except Exception as e:
        print(f" Warning: Could not update version in GUI file: {e}")

def build_executable(version):
    """Build the standalone executable"""
    print("\n" + "=" * 50)
    print("Building executable...")
    print("=" * 50)
    
    main_script = "tgs_gui.py"
    if not os.path.exists(main_script):
        print(f" Error: {main_script} not found!")
        return False
    
    safe_version = sanitize_version_for_filename(version)
    icon_path = os.path.abspath("dihomodyne_beams_icon.ico")
    
    icon_param = []
    if os.path.exists(icon_path):
        print(f" Using icon: {icon_path}")
        icon_param = [f"--icon={icon_path}"]
    else:
        print(" Warning: Icon not found, continuing without icon...")
    
    # More thorough cleanup of previous build files
    print("\nCleaning previous build files...")
    dirs_to_clean = ['build', 'dist', '__pycache__']
    for dir_name in dirs_to_clean:
        dir_path = Path(dir_name)
        if dir_path.exists():
            try:
                shutil.rmtree(dir_path, ignore_errors=True)
                print(f"  Removed {dir_name}/")
            except Exception as e:
                print(f"  Could not remove {dir_name}: {e}")
    
    for file in Path(".").glob("*.spec"):
        try:
            file.unlink()
            print(f"  Removed: {file.name}")
        except:
            pass
    
    # Build command with additional hidden imports and exclusions
    cmd = [
        sys.executable, "-m", "PyInstaller",
        f"--name=PyTGS_v{safe_version}",
        "--windowed",
        "--onefile",
        f"--icon={icon_path}",
        "--add-data=config.yaml;.",
        f"--add-data={icon_path};.",
        
        # Basic hidden imports
        "--hidden-import=PIL",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageTk",
        "--hidden-import=yaml",
        "--hidden-import=tkinter",
        "--hidden-import=ttkbootstrap",
        
        # Matplotlib - tk backend
        "--hidden-import=matplotlib",
        "--hidden-import=matplotlib.backends.backend_tkagg",
        "--hidden-import=matplotlib.figure",
        "--hidden-import=matplotlib.pyplot",
        
        # NumPy - basic only
        "--hidden-import=numpy",
        "--hidden-import=numpy.core._methods",
        "--hidden-import=numpy.linalg.lapack_lite",
        
        # SciPy - include all dependencies needed for scipy.signal
        "--hidden-import=scipy",
        "--hidden-import=scipy.fft",
        "--hidden-import=scipy.optimize",
        "--hidden-import=scipy.special",
        "--hidden-import=scipy.linalg",
        "--hidden-import=scipy.spatial",
        "--hidden-import=scipy.sparse",
        "--hidden-import=scipy.interpolate",
        "--hidden-import=scipy.signal",
        "--hidden-import=scipy.ndimage",
        "--hidden-import=scipy.stats",
        
        # PyVISA and backends
        "--hidden-import=pyvisa",
        "--hidden-import=pyvisa_py",
        "--hidden-import=pyvisa-py",
        "--hidden-import=pyvisa_py.tcpip",
        "--hidden-import=pyvisa_py.usb",
        "--hidden-import=pyvisa_py.serial",
        
        # Your modules
        "--hidden-import=src.analysis.signal_process",
        "--hidden-import=src.analysis.fft",
        "--hidden-import=src.analysis.lorentzian",
        "--hidden-import=src.analysis.tgs",
        "--hidden-import=src.core.path",
        "--hidden-import=src.core.utils",
        "--hidden-import=src.core.plots",
        "--hidden-import=src.analysis.functions",
        
        # Safe exclusions (test modules, unused GUI backends, etc.)
        "--exclude-module=matplotlib.tests",
        "--exclude-module=numpy.f2py.tests",
        "--exclude-module=scipy._lib.array_api_compat.torch",
        "--exclude-module=pyvisa.testsuite",
        "--exclude-module=pytest",
        "--exclude-module=torch",
        "--exclude-module=PyQt5",
        "--exclude-module=PySide2",
        "--exclude-module=PyQt6",
        "--exclude-module=PySide6",
        "--exclude-module=tkinter.test",
        "--exclude-module=setuptools",
        "--exclude-module=pkg_resources",
        "--exclude-module=distutils",
        "--exclude-module=ctypes.test",
        "--exclude-module=distutils.tests",
        "--exclude-module=lib2to3",
        
        # Collect data for these packages
        "--collect-all=ttkbootstrap",
        "--collect-all=pyvisa",
        "--collect-all=pyvisa_py",
        "--collect-data=pyvisa",
        "--collect-data=pyvisa_py",
        
        "--path", sys.path[0],
        "--noconfirm",
        main_script
    ]
    
    if icon_param:
        onefile_index = cmd.index("--onefile")
        cmd[onefile_index + 1:onefile_index + 1] = icon_param
    
    print("\nRunning PyInstaller...")
    print("This may take a few minutes...\n")
    
    try:
        # Run PyInstaller without capturing output to see real-time progress
        result = subprocess.run(cmd, capture_output=False, text=True)
        
        # Check return code
        if result.returncode != 0:
            print(f"\n PyInstaller returned error code: {result.returncode}")
            return False
        
        # Check if executable was created successfully
        exe_path = Path(f"dist/PyTGS_v{safe_version}.exe")
        
        if exe_path.exists() and exe_path.stat().st_size > 1000000:  # Should be > 1MB
            print(f"\n SUCCESS! Executable built: {exe_path}")
            print(f"  File size: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
            return True
        else:
            print("\n Build failed - executable not found or too small")
            return False
            
    except Exception as e:
        print(f"\n Build failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

def cleanup_old_builds(version):
    """Remove old build artifacts to save space"""
    print("\n" + "=" * 50)
    print("Cleaning up old build files...")
    print("=" * 50)
    
    # Remove build directory with retry logic
    build_dir = Path("build")
    if build_dir.exists():
        try:
            shutil.rmtree(build_dir, ignore_errors=True)
            print(" Removed build directory")
        except Exception as e:
            print(f" Could not remove build directory: {e}")
            print(" Trying alternative cleanup...")
            try:
                import glob
                for item in build_dir.glob('**/*'):
                    try:
                        if item.is_file():
                            item.unlink()
                    except:
                        pass
                shutil.rmtree(build_dir, ignore_errors=True)
                print(" Build directory cleaned")
            except:
                print(" Build directory will be cleaned on next run")
    
    # Remove spec files
    for spec_file in Path(".").glob("*.spec"):
        try:
            spec_file.unlink()
            print(f" Removed: {spec_file.name}")
        except:
            pass
    
    # Remove pycache directories
    for pycache in Path(".").glob("**/__pycache__"):
        try:
            shutil.rmtree(pycache, ignore_errors=True)
        except:
            pass
    
    # Remove old version executables (keep current version)
    safe_version = sanitize_version_for_filename(version)
    dist_dir = Path("dist")
    if dist_dir.exists():
        for exe in dist_dir.glob("PyTGS_*.exe"):
            if f"PyTGS_v{safe_version}.exe" not in str(exe):
                try:
                    exe.unlink()
                    print(f" Removed old executable: {exe.name}")
                except:
                    pass

def check_dependencies():
    """Check if all required packages are installed"""
    print("\n" + "=" * 50)
    print("Checking dependencies...")
    print("=" * 50)
    
    # Map of pip package names to import names
    package_import_map = {
        'pyyaml': 'yaml',
        'numpy': 'numpy',
        'matplotlib': 'matplotlib',
        'scipy': 'scipy',
        'pillow': 'PIL',
        'pyinstaller': None,  # Not needed for runtime, just for building
        'ttkbootstrap': 'ttkbootstrap',
        'pyvisa': 'pyvisa',
        'pyvisa-py': 'pyvisa_py'  # Note: imports with underscore
    }
    
    missing = []
    for package, import_name in package_import_map.items():
        if import_name is None:
            continue
            
        try:
            __import__(import_name)
            print(f"   {package} (imports as {import_name})")
        except ImportError:
            print(f"   {package} (imports as {import_name}) - NOT FOUND")
            missing.append(package)
    
    if missing:
        print(f"\n Warning: Missing packages: {', '.join(missing)}")
        print("  Install with: pip install " + ' '.join(missing))
        response = input("\nContinue anyway? (y/n): ")
        if response.lower() != 'y':
            return False
    else:
        print("\n   All required packages found!")
    
    # Also check for pyinstaller availability (optional)
    try:
        import PyInstaller
        print(f"   pyinstaller (available for building)")
    except ImportError:
        print(f"   pyinstaller not found - building may fail")
    
    return True

def main():
    print("=" * 60)
    print("PyTGS Standalone Builder")
    print("=" * 60)
    print(f"\nBuilding version: {VERSION}")
    
    # Check Python version
    if sys.version_info < (3, 8):
        print(" Error: Python 3.8 or higher is required")
        sys.exit(1)
    
    # Check if running from correct directory
    if not os.path.exists("tgs_gui.py"):
        print(" Error: tgs_gui.py not found in current directory")
        print("  Please run this script from the directory containing tgs_gui.py")
        sys.exit(1)
    
    # Check dependencies
    if not check_dependencies():
        print("\n Build cancelled due to missing dependencies")
        sys.exit(1)
    
    # Update version in GUI
    update_version_in_file(VERSION)
    
    # Build executable
    build_success = build_executable(VERSION)
    
    # Clean up old builds
    cleanup_old_builds(VERSION)
    
    # Final summary
    print("\n" + "=" * 60)
    print("BUILD SUMMARY")
    print("=" * 60)
    
    safe_version = sanitize_version_for_filename(VERSION)
    
    if build_success:
        print(f"\n Version {VERSION} built successfully!")
        print(f"\nOutput file:")
        print(f"  - Executable: dist/PyTGS_v{safe_version}.exe")
        print("\nTo distribute:")
        print(f"  Give users the PyTGS_v{safe_version}.exe file")
        print("  They can run it directly - no extraction needed!")
        print("\nNote for users needing oscilloscope connection:")
        print("  - The executable includes pyvisa-py for pure Python VISA communication")
        print("  - For USB/GPIB support, NI-VISA Runtime may still be required")
        print("  - TCP/IP connections work without additional software")
    else:
        print(f"\n Build failed for version {VERSION}")
        print("\nCommon issues and solutions:")
        print("  1. Make sure all dependencies are installed:")
        print("     pip install pyyaml numpy matplotlib scipy pillow pyinstaller ttkbootstrap pyvisa pyvisa-py")
        print("  2. Check that config.yaml exists in the current directory")
        print("  3. Try building with administrator privileges")
        print("  4. Temporarily disable antivirus software")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nBuild cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)
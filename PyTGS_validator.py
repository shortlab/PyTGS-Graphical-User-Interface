import re
import yaml
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Tuple, Optional, Dict, List
import json
import logging
from datetime import datetime

from src.core.fit import TGSAnalyzer
from src.core.path import Paths
from src.core.utils import read_data
from src.analysis.tgs import tgs_fit
from src.analysis.fft import fft
from src.analysis.lorentzian import lorentzian_fit
from src.analysis.functions import tgs_function

# Tungsten sound speed in m/s
TUNGSTEN_SOUND_SPEED = 2665.9

class CalibratedTGSAnalyzer:
    """Extended TGS analyzer with grating spacing calibration."""
    
    def __init__(self, config: dict, data_root: Path, logger: logging.Logger = None):
        self.config = config
        self.data_root = Path(data_root)
        if logger is not None:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__)
            self._setup_logging()
        
    def _setup_logging(self):
        """Setup logging configuration."""
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            self.logger.addHandler(handler)
    
    def extract_nominal_spacing(self, filename: str) -> Optional[float]:
        """
        Extract nominal grating spacing from filename.
        
        Parameters:
            filename: Filename containing spacing information
            
        Returns:
            Nominal grating spacing in µm, or None if not found
        """
        # Look for patterns like "03.40um", "06.40um", etc.
        pattern = r'(\d+\.\d+)um'
        match = re.search(pattern, filename)
        if match:
            return float(match.group(1))
        return None
    
    def find_file_in_test_data(self, filename: str) -> Optional[Path]:
        """
        Find a file in the test_data directory structure.
        
        Parameters:
            filename: Name of the file to find
            
        Returns:
            Path to the file if found, None otherwise
        """
        # Search recursively in test_data directory
        for file_path in self.data_root.rglob(filename):
            if file_path.is_file():
                return file_path
        
        # If not found, try with just the basename (without calibration subdirectory)
        base_name = Path(filename).name
        for file_path in self.data_root.rglob(base_name):
            if file_path.is_file():
                return file_path
        
        return None
    
    def calibrate_grating_spacing(self, pos_file: Path, neg_file: Path, nominal_spacing: float) -> Tuple[float, float, float]:
        """
        Calibrate grating spacing using tungsten calibration files.
        
        Parameters:
            pos_file: Positive signal file path
            neg_file: Negative signal file path
            nominal_spacing: Nominal grating spacing in µm
            
        Returns:
            Tuple of (calibrated_spacing_um, fitted_frequency_hz, frequency_error_hz)
        """
        self.logger.info(f"Calibrating grating spacing using {pos_file.name} and {neg_file.name}")
        
        # Create temporary paths for calibration
        temp_paths = Paths(
            data_dir=pos_file.parent,
            figure_dir=pos_file.parent / 'figures',
            fit_dir=pos_file.parent / 'fit',
            fit_path=pos_file.parent / 'fit' / 'fit.csv',
            signal_path=pos_file.parent / 'fit' / 'signal.json'
        )
        temp_paths.fit_dir.mkdir(parents=True, exist_ok=True)
        temp_paths.figure_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract the file index (usually 1 for calibration files)
        file_idx = 1
        
        # Create a copy of tgs config without grating_spacing to avoid conflict
        tgs_config = self.config['tgs'].copy()
        tgs_config.pop('grating_spacing', None)  # Remove grating_spacing if it exists
        
        # Run TGS fit with nominal spacing
        try:
            (start_idx, start_time, _, 
             A, A_err, B, B_err, C, C_err, 
             alpha, alpha_err, beta, beta_err, 
             theta, theta_err, tau, tau_err, 
             f, f_err, signal, fft_full, lorentzian_curve) = tgs_fit(
                self.config, temp_paths, file_idx, str(pos_file), str(neg_file), 
                nominal_spacing, **tgs_config
            )
            
            # Calculate calibrated spacing using tungsten sound speed
            calibrated_spacing = TUNGSTEN_SOUND_SPEED / f
            
            # Convert to µm
            calibrated_spacing_um = calibrated_spacing * 1e6
            
            # Calculate calibrated spacing error
            spacing_error = (TUNGSTEN_SOUND_SPEED / (f**2)) * f_err * 1e6
            
            self.logger.info(f"Nominal spacing: {nominal_spacing:.4f} µm, "
                           f"Fitted frequency: {f/1e9:.6f} GHz, "
                           f"Calibrated spacing: {calibrated_spacing_um:.4f} µm "
                           f"(±{spacing_error:.4f} µm)")
            
            return calibrated_spacing_um, f, f_err
            
        except Exception as e:
            self.logger.error(f"Calibration failed for {pos_file.name}: {str(e)}")
            raise
    
    def process_file_pair(self, data_pos: Path, data_neg: Path, calibrated_spacing: float, 
                          output_dir: Path) -> Dict:
        """
        Process a data file pair with calibrated grating spacing.
        
        Parameters:
            data_pos: Positive data file path
            data_neg: Negative data file path
            calibrated_spacing: Calibrated grating spacing in µm
            output_dir: Directory to save output
            
        Returns:
            Dictionary containing fit results
        """
        self.logger.info(f"Processing data files: {data_pos.name} and {data_neg.name}")
        
        # Create paths for this run
        run_paths = Paths(
            data_dir=data_pos.parent,
            figure_dir=output_dir / 'figures',
            fit_dir=output_dir / 'fit',
            fit_path=output_dir / 'fit' / 'fit.csv',
            signal_path=output_dir / 'fit' / 'signal.json'
        )
        run_paths.fit_dir.mkdir(parents=True, exist_ok=True)
        run_paths.figure_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract file index from filename
        # Look for patterns like "POS-1", "POS-2", etc.
        pattern = r'POS-(\d+)'
        match = re.search(pattern, data_pos.name)
        if match:
            file_idx = int(match.group(1))
        else:
            file_idx = 1
        
        # Create a copy of tgs config without grating_spacing to avoid conflict
        tgs_config = self.config['tgs'].copy()
        tgs_config.pop('grating_spacing', None)  # Remove grating_spacing if it exists
        
        # Run TGS fit with calibrated spacing
        try:
            (start_idx, start_time, _, 
             A, A_err, B, B_err, C, C_err, 
             alpha, alpha_err, beta, beta_err, 
             theta, theta_err, tau, tau_err, 
             f, f_err, signal, fft_full, lorentzian_curve) = tgs_fit(
                self.config, run_paths, file_idx, str(data_pos), str(data_neg), 
                calibrated_spacing, **tgs_config
            )
            
            results = {
                'run_name': data_pos.name.replace('-POS', '').replace('.txt', ''),
                'date_time': datetime.now().strftime('%Y-%m-%d_%H:%M:%S'),
                'grating_spacing_um': calibrated_spacing,
                'SAW_freq_Hz': f,
                'SAW_freq_error_Hz': f_err,
                'A_Wm-2': A,
                'A_err_Wm-2': A_err,
                'alpha_m2s-1': alpha,
                'alpha_err_m2s-1': alpha_err,
                'beta_s0.5': beta,
                'beta_err_s0.5': beta_err,
                'B_Wm-2': B,
                'B_err_Wm-2': B_err,
                'theta_rad': theta,
                'theta_err_rad': theta_err,
                'tau_s': tau,
                'tau_err_s': tau_err,
                'C_Wm-2': C,
                'C_err_Wm-2': C_err
            }
            
            self.logger.info(f"Successfully processed {data_pos.name}")
            return results
            
        except Exception as e:
            self.logger.error(f"Failed to process {data_pos.name}: {str(e)}")
            raise

def parse_file_pairs(list_file: Path, data_root: Path) -> List[Tuple[Path, Path, Path, Path, float]]:
    """
    Parse the test_data_list.txt file to extract file pairs.
    
    Parameters:
        list_file: Path to the test_data_list.txt file
        data_root: Root directory containing the test data
        
    Returns:
        List of tuples: (data_pos, data_neg, calib_pos, calib_neg, nominal_spacing)
    """
    file_pairs = []
    
    with open(list_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            # Split by spaces (there are multiple spaces between files)
            parts = re.split(r'\s+', line)
            if len(parts) >= 2:
                # First part: data file pair (POS or NEG)
                # Second part: calibration file pair
                data_file = parts[0]
                calib_file = parts[1]
                
                # Determine if it's POS or NEG and find the matching file
                if 'POS' in data_file:
                    data_pos_name = data_file
                    data_neg_name = data_file.replace('POS', 'NEG')
                    calib_pos_name = calib_file
                    calib_neg_name = calib_file.replace('POS', 'NEG')
                else:
                    data_neg_name = data_file
                    data_pos_name = data_file.replace('NEG', 'POS')
                    calib_neg_name = calib_file
                    calib_pos_name = calib_file.replace('NEG', 'POS')
                
                # Find files in test_data directory
                data_pos = find_file_in_directory(data_root, data_pos_name)
                data_neg = find_file_in_directory(data_root, data_neg_name)
                calib_pos = find_file_in_directory(data_root, calib_pos_name)
                calib_neg = find_file_in_directory(data_root, calib_neg_name)
                
                # Extract nominal spacing from data filename
                pattern = r'(\d+\.\d+)um'
                match = re.search(pattern, data_file)
                nominal_spacing = float(match.group(1)) if match else 3.4
                
                if all(p is not None for p in [data_pos, data_neg, calib_pos, calib_neg]):
                    file_pairs.append((data_pos, data_neg, calib_pos, calib_neg, nominal_spacing))
                else:
                    missing = []
                    for name, path in [('data_pos', data_pos), ('data_neg', data_neg), 
                                     ('calib_pos', calib_pos), ('calib_neg', calib_neg)]:
                        if path is None:
                            missing.append(name)
                    print(f"Warning: Missing files for {data_file}: {missing}")
    
    return file_pairs

def find_file_in_directory(root_dir: Path, filename: str) -> Optional[Path]:
    """
    Find a file in the directory structure.
    
    Parameters:
        root_dir: Root directory to search
        filename: Name of the file to find
        
    Returns:
        Path to the file if found, None otherwise
    """
    # Try direct path first
    if (root_dir / filename).exists():
        return root_dir / filename
    
    # Search recursively
    for file_path in root_dir.rglob(filename):
        if file_path.is_file():
            return file_path
    
    # Try with just the basename
    base_name = Path(filename).name
    for file_path in root_dir.rglob(base_name):
        if file_path.is_file():
            return file_path
    
    return None

def compare_to_reference(results_df: pd.DataFrame, reference_file: Path, logger: logging.Logger) -> pd.DataFrame:
    """
    Compare test results to reference data and print a color-coded console report.
    
    Parameters:
        results_df: DataFrame with test results
        reference_file: Path to reference CSV file
        logger: Logger instance
        
    Returns:
        DataFrame with comparison results and discrepancy flags
    """
    # ANSI color codes for console output
    RED = '\033[91m'
    # Using 8-bit color for orange (208 is orange in 256-color mode)
    ORANGE = '\033[38;5;208m'
    YELLOW = '\033[93m'
    GREEN = '\033[92m'
    RESET = '\033[0m'
    BOLD = '\033[1m'
    
    # Load reference data
    ref_df = pd.read_csv(reference_file)
    
    # Clean up run_name to match between test and reference
    results_df['run_name_clean'] = results_df['run_name'].str.replace(r'-1$', '', regex=True)
    ref_df['run_name_clean'] = ref_df['run_name'].str.replace(r'-1$', '', regex=True)
    
    # Deduplicate reference data - keep only the first occurrence of each run
    ref_df = ref_df.drop_duplicates(subset=['run_name_clean'], keep='first')
    
    # Also deduplicate results data to be safe
    results_df = results_df.drop_duplicates(subset=['run_name_clean'], keep='first')
    
    # Parameters to compare
    compare_params = [
        'grating_spacing_um', 'SAW_freq_Hz', 'A_Wm-2', 'alpha_m2s-1', 
        'beta_s0.5', 'B_Wm-2', 'theta_rad', 'tau_s', 'C_Wm-2'
    ]
    
    # Create comparison DataFrame
    comparison_rows = []
    
    for idx, row in results_df.iterrows():
        run_name = row['run_name_clean']
        
        # Find matching reference row
        ref_match = ref_df[ref_df['run_name_clean'] == run_name]
        
        if ref_match.empty:
            logger.warning(f"No reference data found for {run_name}")
            continue
        
        ref_row = ref_match.iloc[0]
        
        comp_row = {'run_name': row['run_name']}
        
        for param in compare_params:
            if param in row and param in ref_row:
                test_val = row[param]
                ref_val = ref_row[param]
                ref_err = ref_row.get(f"{param}_err", 0) if f"{param}_err" in ref_row else 0
                
                # Calculate percentage difference
                if ref_val != 0:
                    pct_diff = ((test_val - ref_val) / ref_val) * 100
                else:
                    pct_diff = np.nan
                
                # Determine discrepancy level
                discrepancy = 'OK'
                if not np.isnan(pct_diff):
                    abs_pct = abs(pct_diff)
                    # Check if difference exceeds reference error first (highest priority)
                    if ref_err != 0 and abs(test_val - ref_val) > ref_err:
                        discrepancy = 'RED'
                    elif abs_pct > 2:
                        discrepancy = 'ORANGE'
                    elif abs_pct > 1:
                        discrepancy = 'YELLOW'
                
                comp_row[f'{param}_test'] = test_val
                comp_row[f'{param}_ref'] = ref_val
                comp_row[f'{param}_pct_diff'] = pct_diff
                comp_row[f'{param}_discrepancy'] = discrepancy
                
        comparison_rows.append(comp_row)
    
    comp_df = pd.DataFrame(comparison_rows)
    
    # Print console report with colors
    print("\n" + "="*120)
    print(f"{BOLD}VALIDATION REPORT - CONSOLE VIEW{RESET}")
    print("="*120)
    print(f"{BOLD}Color Legend:{RESET}")
    print(f"{GREEN}GREEN{RESET}: OK (within 1% and within error bars)")
    print(f"{YELLOW}YELLOW{RESET}: >1% difference")
    print(f"{ORANGE}ORANGE{RESET}: >2% difference")
    print(f"{RED}RED{RESET}: Exceeds reference error")
    print("-"*120)
    
    # Print header
    header = f"{'Run Name':<50}"
    for param in compare_params:
        # Shorten parameter names for display
        short_name = param.replace('_um', '').replace('_Hz', '').replace('_Wm-2', '').replace('_m2s-1', '').replace('_s0.5', '').replace('_rad', '').replace('_s', '')
        header += f"{short_name[:10]:<12} "
    print(header)
    print("-"*120)
    
    # Print each row with colors
    for _, row in comp_df.iterrows():
        row_str = f"{row['run_name'][:50]:<50}"
        
        for param in compare_params:
            pct_diff = row.get(f'{param}_pct_diff', np.nan)
            discrepancy = row.get(f'{param}_discrepancy', 'OK')
            
            # Choose color
            if discrepancy == 'RED':
                color = RED
            elif discrepancy == 'ORANGE':
                color = ORANGE
            elif discrepancy == 'YELLOW':
                color = YELLOW
            else:
                color = GREEN
            
            # Format value
            if not np.isnan(pct_diff):
                val_str = f"{pct_diff:>7.1f}%"
            else:
                val_str = "   N/A"
            
            row_str += f"{color}{val_str:<12}{RESET}"
        
        print(row_str)
    
    print("-"*120)
    
    # Print summary
    total_runs = len(comp_df)
    
    # Count discrepancies across all parameters
    red_count = 0
    orange_count = 0
    yellow_count = 0
    
    for param in compare_params:
        disc_col = f'{param}_discrepancy'
        if disc_col in comp_df.columns:
            red_count += len(comp_df[comp_df[disc_col] == 'RED'])
            orange_count += len(comp_df[comp_df[disc_col] == 'ORANGE'])
            yellow_count += len(comp_df[comp_df[disc_col] == 'YELLOW'])
    
    print(f"{BOLD}Summary:{RESET}")
    print(f"  Total runs compared: {total_runs}")
    print(f"  {RED}Red{RESET} discrepancies (exceeds reference error): {red_count}")
    print(f"  {ORANGE}Orange{RESET} discrepancies (>2% difference): {orange_count}")
    print(f"  {YELLOW}Yellow{RESET} discrepancies (>1% difference): {yellow_count}")
    
    # Print per-parameter summary
    print(f"\n{BOLD}Per-Parameter Summary:{RESET}")
    for param in compare_params:
        disc_col = f'{param}_discrepancy'
        if disc_col in comp_df.columns:
            param_red = len(comp_df[comp_df[disc_col] == 'RED'])
            param_orange = len(comp_df[comp_df[disc_col] == 'ORANGE'])
            param_yellow = len(comp_df[comp_df[disc_col] == 'YELLOW'])
            param_ok = len(comp_df[comp_df[disc_col] == 'OK'])
            
            short_name = param.replace('_um', '').replace('_Hz', '').replace('_Wm-2', '').replace('_m2s-1', '').replace('_s0.5', '').replace('_rad', '').replace('_s', '')
            print(f"  {short_name:<20}: OK: {param_ok:2d}, {YELLOW}Y:{param_yellow:2d}{RESET}, {ORANGE}O:{param_orange:2d}{RESET}, {RED}R:{param_red:2d}{RESET}")
    
    print("="*120 + "\n")
    
    return comp_df

def main():
    """Main function to process all file pairs."""
    # Load configuration
    with open('config.yaml', 'r') as file:
        config = yaml.safe_load(file)
    
    # Disable all plotting to avoid generating figures
    if 'plot' in config:
        config['plot']['signal_process'] = False
        config['plot']['fft_lorentzian'] = False
        config['plot']['tgs'] = False
    
    # Setup logging - use a single logger without basicConfig
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    # Clear any existing handlers to avoid duplicates
    if logger.handlers:
        logger.handlers.clear()
    
    # Add a single console handler
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)
    
    # Prevent propagation to root logger to avoid duplicates
    logger.propagate = False
    
    # Define paths
    data_root = Path('test_data')
    list_file = Path('test_data_list.txt')
    
    # Check if test_data_list.txt exists in the current directory
    # If not, try looking in the test_data folder
    if not list_file.exists():
        list_file = data_root / 'test_data_list.txt'
        if not list_file.exists():
            logger.error(f"File test_data_list.txt not found in current directory or in test_data folder!")
            return
    
    logger.info(f"Using file list: {list_file}")
    logger.info("Plotting disabled for batch processing")
    
    # Parse file pairs
    file_pairs = parse_file_pairs(list_file, data_root)
    logger.info(f"Found {len(file_pairs)} file pairs to process")
    
    # Initialize analyzer with the same logger
    analyzer = CalibratedTGSAnalyzer(config, data_root, logger=logger)
    
    # Process each pair
    all_results = []
    calibration_results = []
    
    for idx, (data_pos, data_neg, calib_pos, calib_neg, nominal_spacing) in enumerate(file_pairs, 1):
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing pair {idx}/{len(file_pairs)}")
        logger.info(f"Data: {data_pos.name}")
        logger.info(f"Calib: {calib_pos.name}")
        logger.info(f"Nominal spacing: {nominal_spacing:.4f} µm")
        logger.info(f"{'='*60}")
        
        try:
            # Calibrate grating spacing using tungsten files
            calibrated_spacing, calib_freq, calib_freq_err = analyzer.calibrate_grating_spacing(
                calib_pos, calib_neg, nominal_spacing
            )
            
            # Store calibration results
            calib_result = {
                'data_file': data_pos.name,
                'nominal_spacing_um': nominal_spacing,
                'calibrated_spacing_um': calibrated_spacing,
                'calibration_freq_Hz': calib_freq,
                'calibration_freq_error_Hz': calib_freq_err
            }
            calibration_results.append(calib_result)
            
            # Create output directory for this run inside test_data/calibrated_results
            base_name = data_pos.name.replace('-POS', '').replace('.txt', '')
            output_dir = data_root / 'calibrated_results' / base_name
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Process data files with calibrated spacing
            results = analyzer.process_file_pair(
                data_pos, data_neg, calibrated_spacing, output_dir
            )
            
            # Add calibration info to results
            results['nominal_spacing_um'] = nominal_spacing
            results['calibration_spacing_um'] = calibrated_spacing
            
            all_results.append(results)
            
            # Save individual postprocessing file
            post_file = output_dir / f"{base_name}_postprocessing.txt"
            
            # Write in the format of the example
            with open(post_file, 'w') as f:
                # Write header
                headers = ['run_name', 'date_time', 'grating_spacing_um', 'SAW_freq_Hz', 
                          'SAW_freq_error_Hz', 'A_Wm-2', 'A_err_Wm-2', 'alpha_m2s-1', 
                          'alpha_err_m2s-1', 'beta_s0.5', 'beta_err_s0.5', 'B_Wm-2', 
                          'B_err_Wm-2', 'theta_rad', 'theta_err_rad', 'tau_s', 'tau_err_s', 
                          'C_Wm-2', 'C_err_Wm-2']
                f.write(' '.join(headers) + '\n')
                
                # Write data row
                values = []
                for header in headers:
                    if header in results:
                        if isinstance(results[header], float):
                            values.append(f"{results[header]:.8e}")
                        else:
                            values.append(str(results[header]))
                    else:
                        values.append('')
                f.write(' '.join(values) + '\n')
            
            logger.info(f"Results saved to {post_file}")
            
        except Exception as e:
            logger.error(f"Error processing pair {idx}: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            continue
    
    # Save all results to a single CSV file
    if all_results:
        # Create output directory for combined results inside test_data
        output_dir = data_root / 'calibrated_results'
        output_dir.mkdir(exist_ok=True)
        
        # Save calibration summary
        if calibration_results:
            calib_df = pd.DataFrame(calibration_results)
            calib_file = output_dir / 'calibration_summary.csv'
            calib_df.to_csv(calib_file, index=False, float_format='%.8e')
            logger.info(f"Calibration summary saved to {calib_file}")
        
        # Save processing results
        results_df = pd.DataFrame(all_results)
        
        # Reorder columns to match the example
        column_order = [
            'run_name', 'date_time', 'grating_spacing_um', 'nominal_spacing_um',
            'calibration_spacing_um', 'SAW_freq_Hz', 'SAW_freq_error_Hz',
            'A_Wm-2', 'A_err_Wm-2',
            'alpha_m2s-1', 'alpha_err_m2s-1',
            'beta_s0.5', 'beta_err_s0.5',
            'B_Wm-2', 'B_err_Wm-2',
            'theta_rad', 'theta_err_rad',
            'tau_s', 'tau_err_s',
            'C_Wm-2', 'C_err_Wm-2'
        ]
        
        # Reorder columns if they exist
        existing_cols = [col for col in column_order if col in results_df.columns]
        results_df = results_df[existing_cols]
        
        # Save to CSV
        output_file = output_dir / 'all_processed_results.csv'
        results_df.to_csv(output_file, index=False, float_format='%.8e')
        
        logger.info(f"\nResults saved to {output_file}")
        logger.info(f"Total successful fits: {len(all_results)} out of {len(file_pairs)} pairs")
        
        # Compare to reference data if available
        reference_file = data_root / 'reference_processed_results.csv'
        if reference_file.exists():
            logger.info("\n" + "="*60)
            logger.info("Comparing results to reference data...")
            logger.info("="*60)
            
            try:
                comp_df = compare_to_reference(results_df, reference_file, logger)
                logger.info(f"Comparison complete. Found {len(comp_df)} matching runs.")
            except Exception as e:
                logger.error(f"Error comparing to reference: {str(e)}")
                import traceback
                logger.error(traceback.format_exc())
        else:
            logger.warning(f"Reference file {reference_file} not found. Skipping comparison.")
        
    else:
        logger.warning("No results were generated!")

if __name__ == "__main__":
    main()
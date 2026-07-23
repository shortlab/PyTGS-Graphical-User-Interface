# PyTGS
Repository for a GUI that handles acquisition, processing and display of di-homodyne transient grating signals. Based on the PyTGS script created by A. Aurora and stemming from scripts by C. A. Dennett, B.R. Dacus, A.P.C. Wylie, E. Botica Artalejo, K. Zoubkova and S. Engebretson. 

To run on Windows, download and open the executable file in the dist folder. For now, a popup will appear warning you against unsigned software, you may click advanced and run anyway to bypass this. 

For Python users on Windows and elsewhere:

## Prerequisites

- Python 3.8 - 3.12 ([download](https://www.python.org/downloads/))
- Git ([download](https://git-scm.com/downloads))

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/shortlab/PyTGS.git
   cd PyTGS
   ```

2. Run the automated setup script:
   
   **On Unix/MacOS:**
   ```bash
   chmod +x setup.sh
   ./setup.sh
   ```

   **On Windows:**
   ```bash
   bash setup.sh
   ```
   
   Note: On Windows, you'll need Git Bash or a similar bash shell. If you don't have one use the manual installation below.

   **Manual Installation (Windows/Unix/MacOS):**
   ```bash
   python -m venv PyTGS-venv
   # On Windows:
   PyTGS-venv\Scripts\activate
   # On Unix/MacOS:
   source PyTGS-venv/bin/activate
   
   pip install --upgrade pip
   pip install -e .  # or pip install -r requirements.txt
   ```

## Usage

1. Activate the virtual environment:
   
   **On Unix/MacOS:**
   ```bash
   source PyTGS-venv/bin/activate
   ```
   
   **On Windows:**
   ```bash
   PyTGS-venv\Scripts\activate
   ```

2. Run the gui:
   ```bash
   python tgs_gui.py
   ```

   Fitting results and figures will be saved in `fit/` and `figures/` directories, respectively.
   You can view example input/output files in the `example/` directory.

## Testing

The package includes tests that validate correctness of the analysis pipeline using synthetic TGS signals. 

To run the tests, use the following command:
```bash
python TGS_validator.py
```
Which will run through the 11 pairs of reference data, calibrate and then process these data using the local scripts and provide a report of any discrepancies.

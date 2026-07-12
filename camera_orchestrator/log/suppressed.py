"""Third-party warning suppressions.

Collected here so they're applied once at import time and easy to audit.
Each suppression should document why it's safe to ignore.
"""
import warnings

# astropy emits this on every WCS parse when the FITS header has no NAXIS
# but the WCS has 2 axes — harmless for our use case (we only read RA/Dec centre).
warnings.filterwarnings("ignore", message=".*FITSFixedWarning.*")
warnings.filterwarnings("ignore", message=".*WCS transformation has more axes.*")

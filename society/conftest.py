# Ensures society/ is on sys.path for pytest so the functional subpackages
# (core/, ...) and any still-flat modules resolve uniformly. (#58 restructure)
import os, sys
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

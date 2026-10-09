"""Test-process Qt binding selection.

This runs before test-module imports, including direct pyFAI imports.
"""

import os


os.environ["QT_API"] = "pyside6"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

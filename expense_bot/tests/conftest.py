"""Ensure the project root (expense_bot/) is importable as top-level modules."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

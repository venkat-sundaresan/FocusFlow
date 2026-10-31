# Double-click (or autostart) launcher: runs FocusFlow with no console window.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from focusflow.app import main

main()

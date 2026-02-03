"""
Passenger WSGI entry point for shared hosting (cPanel, Passenger, etc.).
Passenger looks for this file and for the 'application' object.
"""
import sys
import os

# Ensure the app's directory is on the path and is the current working directory
# (so Flask finds templates, static files, and imports work)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from app import app

# Passenger expects the WSGI callable to be named 'application'
application = app

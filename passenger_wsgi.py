"""
Passenger WSGI entry point for shared hosting (cPanel, Passenger, etc.).
Passenger looks for this file and for the 'application' object.
"""
import sys
import os

# Optional: add your project directory to the path (if not already there)
# sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app

# Passenger expects the WSGI callable to be named 'application'
application = app

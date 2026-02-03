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

# When the app is in a subfolder (e.g. /rush_tech), the server may pass PATH_INFO like /rush_tech/login.
# Strip APPLICATION_ROOT so Flask sees /login and routes match (avoids 404).
# Set APPLICATION_ROOT=/rush_tech in env or .env (no trailing slash).
_application_root = (os.environ.get('APPLICATION_ROOT') or '').strip()
if _application_root and not _application_root.startswith('/'):
    _application_root = '/' + _application_root

def _application(environ, start_response):
    if _application_root and environ.get('PATH_INFO', '').startswith(_application_root):
        prefix = _application_root.rstrip('/')
        path = environ['PATH_INFO']
        if path == prefix or path == prefix + '/':
            environ['PATH_INFO'] = '/'
        elif path.startswith(prefix + '/'):
            environ['PATH_INFO'] = path[len(prefix):]
        else:
            environ['PATH_INFO'] = path[len(prefix):] if path.startswith(prefix) else path
        environ['SCRIPT_NAME'] = prefix
    return app(environ, start_response)

# Passenger expects the WSGI callable to be named 'application'
application = _application if _application_root else app

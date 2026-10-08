"""Entry point for cPanel 'Setup Python App' (Passenger). Startup file: passenger_wsgi.py, entry point: application."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import create_app
application = create_app()

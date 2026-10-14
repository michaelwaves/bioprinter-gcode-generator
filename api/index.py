"""
Vercel serverless entry point.

Vercel's Python runtime looks for a WSGI/ASGI callable named `app` in this module and
serves it; vercel.json rewrites every path here so Flask does its own routing. The repo
root is added to sys.path because the function's working directory is this `api/`
directory, so `app.py` and `line_trace_gcode.py` at the root would not otherwise import.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  -- must follow the sys.path insert above

# Re-exported so the runtime can find it under either name.
application = app

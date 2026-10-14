"""
Vercel serverless entry point.

Vercel's Python runtime looks for a WSGI callable named `app` in this module and serves it;
vercel.json rewrites every path here so Flask does its own routing. The repo root is added
to sys.path because the function's working directory is this `api/` directory, so `app.py`
and `line_trace_gcode.py` at the root would not otherwise import.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must run BEFORE anything imports scikit-image: Vercel's builder strips the `.pyi` stubs
# that scikit-image parses at import time to build its namespace, so without this the
# function dies with "Cannot load imports from non-existent stub". See lazy_stub_fix.
import lazy_stub_fix  # noqa: E402

lazy_stub_fix.install()

from app import app  # noqa: E402  -- must follow the sys.path insert and the stub fix

# Re-exported so the runtime can find it under either name.
application = app

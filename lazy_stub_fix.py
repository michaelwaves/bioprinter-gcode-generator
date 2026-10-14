"""
Make scikit-image importable on hosts that strip `.pyi` files from installed packages.

scikit-image builds its public namespace at import time with
`lazy_loader.attach_stub(__name__, __file__)`, which *parses the package's `__init__.pyi`*
to learn which names live in which submodule. The stub is therefore load-bearing runtime
data, not type-checker metadata -- but it looks like metadata, so bundlers that prune type
stubs to save space delete it. Vercel's Python builder does exactly that, and the function
then dies on import with:

    ValueError: Cannot load imports from non-existent stub
                '/var/task/_vendor/skimage/__init__.pyi'

Fix: ship our own copies of the stubs (see vendor_stubs/) and point `attach_stub` at them
when the installed one is missing. The copies are suffixed `.pyi.txt` so that the same
pruning that caused the problem cannot eat them too, and are materialised into a temp dir
under their real `.pyi` name because `attach_stub` requires a filename ending in "i".

Parsing is still done by lazy_loader itself -- the original function is called with the
new path rather than the stub format being reimplemented here, so this cannot drift from
how scikit-image actually expects its namespace to be built.

The shipped stubs must match the installed scikit-image, so requirements.txt pins it. If
you bump that pin, refresh the stubs:

    SP=.venv/lib/python3.*/site-packages
    find $SP/skimage -name '*.pyi' | while read f; do
      dst="vendor_stubs/${f#$SP/}.txt"; mkdir -p "$(dirname "$dst")"; cp "$f" "$dst"
    done

This is a no-op wherever the real stubs are present, so it is safe to call unconditionally
and it does not affect local runs.
"""

import os
import shutil
import tempfile

STUB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor_stubs")


def install():
    """Patch lazy_loader.attach_stub to fall back to the stubs in vendor_stubs/."""
    try:
        import lazy_loader
    except ImportError:
        return  # nothing using stub-based lazy loading is installed

    original = lazy_loader.attach_stub
    if getattr(original, "_stub_fallback_installed", False):
        return

    cache_root = os.path.join(tempfile.gettempdir(), "vendored_lazy_stubs")

    def attach_stub(package_name, filename):
        try:
            return original(package_name, filename)
        except ValueError:
            parts = package_name.split(".")
            shipped = os.path.join(STUB_DIR, *parts, "__init__.pyi.txt")
            if not os.path.exists(shipped):
                raise  # not one we carry a copy of; the real error is the useful one
            # attach_stub only accepts a path ending in "i", so stage it under that name.
            # On a serverless host the temp dir is the one writable location.
            staged = os.path.join(cache_root, *parts, "__init__.pyi")
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            if not os.path.exists(staged):
                shutil.copyfile(shipped, staged)
            return original(package_name, staged)

    attach_stub._stub_fallback_installed = True
    lazy_loader.attach_stub = attach_stub

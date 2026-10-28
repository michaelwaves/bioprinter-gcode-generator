# Agar Line-Trace Generator — Web UI

A Flask front end for `line_trace_gcode.py`. Upload an image, pick a plate and a
trace mode, get back a preview and a downloadable `.gcode` file sized to that plate.
No processing logic was reimplemented for the web version — it calls the same tested
`process_image()` function the CLI script uses.

## Setup

```
pip install -r requirements.txt --break-system-packages
```
(drop `--break-system-packages` if you're using a virtualenv, which is fine too)

## Run locally

```
python3 app.py
```

Then open http://127.0.0.1:5000 in a browser. Stop it with Ctrl+C.

Flask's auto-reloader is off (`debug=False`), so **code and template edits need a
restart** to take effect. Set `debug=True` in `app.py` if you want hot reload.

## Plate presets

Plates are defined in one place: the `PLATE_PRESETS` dict at the top of
`line_trace_gcode.py`. The web UI builds its dropdown from it and the CLI takes
`--plate <key>`, so adding a plate needs no other edits:

```python
PLATE_PRESETS = {
    "my_plate": {
        "label": "120mm square plate",   # shown in the dropdown
        "shape": "square",               # "square" or "round"
        "size_mm": 120.0,                # FLAT INNER floor, not outer rim-to-rim
        "edge_margin_mm": 3.0,           # ~2mm bead spread + ~1mm buffer
        "agar_volume_ml": 45.0,          # default pour for this plate
        "center_xy": (100.0, 100.0),     # bed coords the plate centre sits at
    },
}
```

Shipped presets: `petri_85`, `petri_100`, `square_150`, `square_200`. `DEFAULT_PLATE_KEY` sets
which one is preselected.

Three things to get right when adding one:

- **`size_mm` is the flat floor, not the nominal size.** Plates with a sloped rim have
  a usable flat base noticeably smaller than their advertised dimension. Measure it.
- **`center_xy` must match where the plate physically sits on the bed.** Everything is
  placed relative to it, and the print-area clip is centred on it — so if it's wrong,
  the clip protects the wrong region.
- **`agar_volume_ml` doesn't carry over between plates.** Floor area grows with the
  *square* of plate size, so a volume that gave a good depth on a small dish is a very
  thin layer on a big plate. 20mL is 3.52mm deep in an 85mm dish and 0.89mm on a 150mm
  square. Unless `default_height_mm` is set on the preset, the default needle height
  is derived from this, so a wrong volume means a wrong Z. Both square presets have
  an explicit **3.2mm** default height; the form allows manual adjustments.

### Square print areas

Square presets use a rectangular path clip, so all four corners are accessible.
Round Petri dishes retain their circular clip. The red preview boundary matches
the actual clip; the dashed outline shows the physical plate edge.

The 3mm margin on each edge gives `square_150` a **144 × 144mm** printable area and
`square_200` a **194 × 194mm** printable area. The 200mm preset assumes a 125mL pour
(about 3.1mm depth) and the same bed center, `(100, 100)`, as the other presets;
its printable X/Y coordinates run from 3 to 197mm. The preset describes plate
geometry; it does not change the printer firmware's travel limits or homing.

### Printer controls and homing

This app generates files and previews; it has no manual Extrude/Retract buttons
or Home All command. Generated files use negative E to extrude and positive E
to retract for this syringe setup. Reversed manual buttons must be investigated
in the printer interface/configuration that provides those controls.

Generated G-code sets millimetres and absolute X/Y positioning, but does not
issue `G28` or reset X/Y coordinates. Repeated Home All offsets cannot be diagnosed
from this repository alone; investigate the printer's homing configuration and
mechanics separately.

## Trace modes

- **both** (hybrid) — outline + skeleton, good general default
- **edge** — traces outlines/borders only
- **skeletonize** — traces centerlines only, best for thin line art

The **Dark-pixel threshold** slider controls which image pixels become printable
foreground before tracing, in all three modes. Images are converted to grayscale
(0 = black, 255 = white); pixels strictly below the selected threshold are included.
The default is 128. Lower values keep only darker marks; higher values include
lighter detail but can also include background noise. In Edge mode, contours follow
the boundaries of those selected regions. This is a brightness cutoff, not a
minimum feature size or a change in tracing resolution (still capped at 300px).

Use Generate or Regenerate to apply the slider. Downloads preserve the threshold
used for their preview. The CLI accepts the same setting as `--dark-threshold 180`.

## Deploying to Vercel

```
npx vercel          # preview deploy
npx vercel --prod   # production
```

Vercel detects the root `app.py` and serves its Flask `app` directly; `vercel.json` only
sets memory and `maxDuration` for it. **Don't add an `api/` entrypoint** — an earlier version
had `api/index.py` and Vercel ignored it, importing `/var/task/app.py` instead.

The app holds **no server-side state** — this is what makes it work on serverless.
Each request carries the working-resolution design as a base64 hidden form field
(~8KB), so `/regenerate` and `/download` never need a stored session or a temp file;
earlier versions kept a `SESSIONS` dict plus a temp dir, which breaks as soon as
requests land on different instances. `/download` re-runs the deterministic pipeline
rather than storing the generated file.

### The scikit-image stub problem

Vercel's Python builder strips `.pyi` files from installed packages. scikit-image builds its
public namespace at import time with `lazy_loader.attach_stub()`, which **parses
`skimage/__init__.pyi`** to learn which names live in which submodule — so for this package
the stub is load-bearing runtime data, not type-checker metadata. Stripped, the function
dies on cold start with:

```
ValueError: Cannot load imports from non-existent stub '/var/task/_vendor/skimage/__init__.pyi'
```

`vendor_stubs/` carries our own copies (60KB) and `lazy_stub_fix.py` points `attach_stub` at
them when the installed stub is missing. Three details that matter if you touch this:

- The copies are suffixed **`.pyi.txt`** so the same stub-pruning cannot eat them too, and
  are staged into a temp dir under their real `.pyi` name because `attach_stub` only accepts
  a path ending in `i`.
- Parsing is still done **by lazy_loader itself** — the original function is called with the
  new path — so this can't drift from how scikit-image expects its namespace built.
- `lazy_stub_fix.install()` is called **inside `line_trace_gcode.py`, immediately above its
  `from skimage import ...`** — deliberately next to the import it protects, not in an entry
  point. It first lived in `api/index.py`, which Vercel never imported (it serves the root
  `app.py`), so the shim silently did nothing and the deploy failed with the stubs sitting
  right there in the bundle. A guard that the host can route around is not a guard. It's a
  no-op where the real stubs exist, so local runs and the CLI are unaffected.

**scikit-image is pinned exactly in requirements.txt** because the shipped stubs must match
the installed version. To bump it, refresh the stubs:

```
SP=.venv/lib/python3.*/site-packages
find $SP/skimage -name '*.pyi' | while read f; do
  dst="vendor_stubs/${f#$SP/}.txt"; mkdir -p "$(dirname "$dst")"; cp "$f" "$dst"
done
```

To verify a fix without deploying, reproduce the host behaviour by stripping the stubs from
a scratch venv and importing the app the way Vercel does (`from app import app`, from a
different working directory):

```
find /path/to/scratch-venv -name '*.pyi' -delete
cd /tmp && /path/to/scratch-venv/bin/python -c \
  "import sys; sys.path.insert(0,'/path/to/repo'); from app import app; print('ok')"
```

Import it through the same entry point the host uses. Testing a different entry point is
what hid this bug the first time.

### Constraints on the free (Hobby) plan

- **Bundle size: ~207MB against a 250MB limit.** scipy and networkx, pulled in by
  scikit-image, are ~117MB of that. matplotlib was removed to make room — it was ~45MB
  and was used only to draw the preview, which is now inline SVG. **Don't add
  matplotlib back**, and check the bundle before adding any large dependency. If it ever
  goes over, the next cut is replacing `skeletonize` / `find_contours` / `label` with
  numpy implementations to drop scikit-image, scipy and networkx (~145MB) — but that
  changes generated paths, so it needs re-verification against physical prints.
- **Upload cap 4MB** (`MAX_UPLOAD_BYTES`), under Vercel's 4.5MB request body limit.
  Tracing downsamples to `MAX_TRACE_SIDE_PX` (300px) anyway, so this costs nothing real.
- **`maxDuration` is 60s** with 1GB memory in `vercel.json`. Cold starts pay a few
  seconds to import numpy/scipy/scikit-image.
- The pinned deps ship `manylinux_2_28` cp312 and cp313 wheels, which Vercel's build image
  satisfies. Note that recent numpy has **no manylinux2014 wheels** — if you pin an older
  Python, check wheel availability before assuming it resolves.

## Notes

- Print heights and extrusion rate live in `line_trace_gcode.py`; form clamp ranges
  (`SCALE_PCT_MIN/MAX`, `OFFSET_MM_LIMIT`, …) live in `app.py`. The templates read their
  input `min`/`max` from those constants via `_form_bounds()`, so widening a clamp takes
  effect without editing HTML. Hardcoding them in both places silently breaks: the
  browser rejects out-of-range values before they're ever submitted.
- `print_height_mm` defaults to the preset's `default_height_mm` when specified
  (3.2mm for square plates). Otherwise it is agar surface height **plus**
  `PRINT_HEIGHT_CLEARANCE_MM` (0.1mm).
- Not hardened for untrusted users. There's no auth, and every request runs image
  processing, so a public deployment is trivially easy to run up against rate limits.

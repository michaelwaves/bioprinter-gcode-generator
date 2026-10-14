#!/usr/bin/env python3
"""
Web UI for the agar bioprinter line-trace pipeline.

Wraps the tested functions in line_trace_gcode.py -- this file only adds upload
handling, the plate/trace-mode selectors, and HTML rendering.

This app holds NO server-side state. Earlier versions kept a SESSIONS dict mapping a
session id to a temp directory with the upload in it, which works on one long-lived local
process but breaks on any serverless host: each request may land on a fresh instance with
its own empty memory and an ephemeral filesystem, so the temp dir and the dict entry are
routinely gone by the time /regenerate or /download arrives. Instead, the working-resolution
copy of the design is round-tripped through a hidden form field, so every request carries
everything it needs to reproduce the result.

Run locally with:  python3 app.py   (then open http://127.0.0.1:5000)
"""

import io
import os
import base64
import binascii

from PIL import Image
from flask import Flask, request, render_template, Response, abort

import line_trace_gcode as ltg

app = Flask(__name__)

ALLOWED_EXTS = {".png", ".jpg", ".jpeg"}
TRACE_MODES = {
    "both": "Hybrid (outline + skeleton) -- best general-purpose default",
    "edge": "Edge only -- traces outlines/borders of shapes",
    "skeletonize": "Skeleton only -- traces centerlines, good for thin line art",
}

SCALE_PCT_MIN, SCALE_PCT_MAX = 10.0, 500.0
OFFSET_MM_LIMIT = 100.0  # generous clamp; the print-area clip catches the real violation
PRINT_HEIGHT_MM_MIN, PRINT_HEIGHT_MM_MAX = 0, 15.0  # sane physical bounds for a needle Z height
EXTRUSION_PER_MM_MIN, EXTRUSION_PER_MM_MAX = 0.0005, 0.05  # mm^3/mm -- wide enough for recalibration

# Vercel caps a serverless request body at 4.5MB, and the browser gives no useful error when
# that limit is hit -- the upload just fails. Rejecting earlier, with a readable message, is
# the difference between "too big, resize it" and an opaque failure.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def _clamped_float(form, name, default, lo, hi):
    raw = form.get(name, "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, val))


def _selected_plate(form):
    """Resolve the submitted plate preset, falling back to the default on anything unknown."""
    key = form.get("plate_key", ltg.DEFAULT_PLATE_KEY)
    if key not in ltg.PLATE_PRESETS:
        key = ltg.DEFAULT_PLATE_KEY
    return ltg.Plate.from_key(key)


def _parse_placement_fields(form, plate):
    """
    Parse scale_pct/offset_x_mm/offset_y_mm/print_height_mm/extrusion_per_mm from a form,
    clamping to sane ranges. Falls back to defaults on missing or unparseable values rather
    than erroring -- these are refinement controls, not required input.

    print_height_mm defaults per plate: the agar surface height depends on the plate's floor
    area, so the same pour sits at a different height on a different preset.
    """
    scale_pct = _clamped_float(form, "scale_pct", 100.0, SCALE_PCT_MIN, SCALE_PCT_MAX)
    offset_x_mm = _clamped_float(form, "offset_x_mm", 0.0, -OFFSET_MM_LIMIT, OFFSET_MM_LIMIT)
    offset_y_mm = _clamped_float(form, "offset_y_mm", 0.0, -OFFSET_MM_LIMIT, OFFSET_MM_LIMIT)
    print_height_mm = _clamped_float(form, "print_height_mm", plate.default_print_height_mm(),
                                      PRINT_HEIGHT_MM_MIN, PRINT_HEIGHT_MM_MAX)
    extrusion_per_mm = _clamped_float(form, "extrusion_per_mm", ltg.DEFAULT_EXTRUSION_PER_MM,
                                       EXTRUSION_PER_MM_MIN, EXTRUSION_PER_MM_MAX)
    return scale_pct, offset_x_mm, offset_y_mm, print_height_mm, extrusion_per_mm


def _working_copy_b64(img):
    """
    Downscale the upload to the tracing working resolution and return it as base64 PNG.

    This is what gets round-tripped through the form so later requests need no server state.
    It stays the GRAYSCALE source rather than a thresholded mask, because the trace mode can
    be changed on the result page and "skeletonize" has to re-derive its mask from the
    original greys. At MAX_TRACE_SIDE_PX the payload is tens of KB, and the pipeline would
    downsample to this size anyway, so nothing is lost that the trace would have used.
    """
    img = img.convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(white, img).convert("L")

    cap = ltg.MAX_TRACE_SIDE_PX
    if max(img.size) > cap:
        scale = cap / float(max(img.size))
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _image_from_b64(raw_b64):
    if not raw_b64:
        abort(400, "The design is missing from this request -- please upload the image again.")
    try:
        return Image.open(io.BytesIO(base64.b64decode(raw_b64, validate=True)))
    except (binascii.Error, ValueError, OSError):
        abort(400, "The design in this request could not be read -- please upload it again.")


def _form_bounds():
    """
    The clamp ranges above, handed to the templates so the form's min/max attributes come
    from these constants instead of being retyped in the HTML. Without this, widening a
    clamp here has no visible effect: the browser rejects the value before it is ever
    submitted, so the wider server-side range is never reached.
    """
    return dict(
        scale_pct_min=f"{SCALE_PCT_MIN:g}",
        scale_pct_max=f"{SCALE_PCT_MAX:g}",
        offset_mm_min=f"{-OFFSET_MM_LIMIT:g}",
        offset_mm_max=f"{OFFSET_MM_LIMIT:g}",
        print_height_mm_min=f"{PRINT_HEIGHT_MM_MIN:g}",
        print_height_mm_max=f"{PRINT_HEIGHT_MM_MAX:g}",
        extrusion_per_mm_min=f"{EXTRUSION_PER_MM_MIN:g}",
        extrusion_per_mm_max=f"{EXTRUSION_PER_MM_MAX:g}",
        max_upload_mb=f"{MAX_UPLOAD_BYTES / (1024 * 1024):g}",
        plates=[ltg.Plate.from_key(k).as_template_dict() for k, _ in ltg.plate_choices()],
    )


def _index_defaults(plate=None):
    plate = plate or ltg.DEFAULT_PLATE
    return dict(
        plate=plate.as_template_dict(),
        selected_plate_key=plate.key,
        default_print_height_mm=f"{plate.default_print_height_mm():.2f}",
        default_extrusion_per_mm=f"{ltg.DEFAULT_EXTRUSION_PER_MM:.4f}",
        **_form_bounds(),
    )


def _render_index(error=None, plate=None):
    return render_template("index.html", trace_modes=TRACE_MODES,
                            error=error, **_index_defaults(plate))


def _run(img, trace_mode, plate, form):
    scale_pct, offset_x_mm, offset_y_mm, print_height_mm, extrusion_per_mm = \
        _parse_placement_fields(form, plate)
    return ltg.process_image(
        img, trace_mode=trace_mode, plate=plate,
        scale_pct=scale_pct, offset_x_mm=offset_x_mm, offset_y_mm=offset_y_mm,
        print_height_mm=print_height_mm, extrusion_per_mm=extrusion_per_mm,
        write_files=False,
    )


@app.errorhandler(413)
def too_large(_exc):
    return _render_index(error=f"That image is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)}MB. "
                                "Resize or re-save it smaller and try again."), 413


@app.route("/", methods=["GET"])
def index():
    return _render_index()


@app.route("/generate", methods=["POST"])
def generate():
    f = request.files.get("image")
    trace_mode = request.form.get("trace_mode", "both")
    plate = _selected_plate(request.form)

    if trace_mode not in TRACE_MODES:
        abort(400, "Unknown trace mode.")
    if f is None or f.filename == "":
        return _render_index(error="Please choose an image file.", plate=plate)

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return _render_index(error=f"Unsupported file type '{ext}'. Use PNG or JPEG.", plate=plate)

    try:
        img = Image.open(f.stream)
        design_b64 = _working_copy_b64(img)
    except OSError:
        return _render_index(error="That file could not be read as an image.", plate=plate)

    try:
        result = _run(_image_from_b64(design_b64), trace_mode, plate, request.form)
    except Exception as exc:
        return _render_index(error=f"Processing failed: {exc}", plate=plate)

    return render_template("result.html", trace_mode=trace_mode, trace_modes=TRACE_MODES,
                            design_b64=design_b64, **_result_template_args(result))


@app.route("/regenerate", methods=["POST"])
def regenerate():
    trace_mode = request.form.get("trace_mode", "both")
    plate = _selected_plate(request.form)
    if trace_mode not in TRACE_MODES:
        abort(400, "Unknown trace mode.")

    design_b64 = request.form.get("design_b64", "")
    img = _image_from_b64(design_b64)

    try:
        result = _run(img, trace_mode, plate, request.form)
    except Exception as exc:
        return _render_index(error=f"Processing failed: {exc}", plate=plate)

    return render_template("result.html", trace_mode=trace_mode, trace_modes=TRACE_MODES,
                            design_b64=design_b64, **_result_template_args(result))


@app.route("/download", methods=["POST"])
def download_gcode():
    """
    Regenerate from the posted parameters and stream the G-code back.

    The pipeline is deterministic for a given (design, plate, placement), so recomputing
    here returns byte-identical G-code to what the preview was built from. That avoids
    needing anywhere to store the generated file between the two requests.
    """
    trace_mode = request.form.get("trace_mode", "both")
    plate = _selected_plate(request.form)
    if trace_mode not in TRACE_MODES:
        abort(400, "Unknown trace mode.")

    img = _image_from_b64(request.form.get("design_b64", ""))
    result = _run(img, trace_mode, plate, request.form)

    return Response(
        result["gcode_text"],
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="line_trace_{plate.key}.gcode"'},
    )


def _result_template_args(result):
    """Shared formatting from a process_image() result dict into result.html's template vars."""
    plate = result["plate"]
    warnings = []
    if result["was_clipped"]:
        warnings.append(f"{result['clipped_len_mm']:.1f}mm of the traced path fell outside the "
                         f"{plate.print_area_radius_mm:.1f}mm print area and was clipped -- part of the "
                         f"design won't print. Reduce scale or offset to fit it all.")
    if not result["fits_dish"]:
        warnings.append(f"This placement reaches {result['farthest_corner_mm']:.1f}mm from plate center "
                         f"-- farther out than the {result['baseline_farthest_mm']:.1f}mm the default "
                         f"auto-fit placement uses for this image.")
    if result["fg_fraction"] > 0.9:
        warnings.append("Over 90% of the image counted as foreground -- the background may also be dark. "
                         "Check the preview before printing.")
    if result["n_paths"] == 0:
        warnings.append("No printable paths were found. Try a different trace mode, or check that your "
                         "design is dark-on-light.")

    return dict(
        preview_svg=result["preview_svg"],
        plate=plate.as_template_dict(),
        selected_plate_key=plate.key,
        n_paths=result["n_paths"],
        fg_fraction=f"{result['fg_fraction']*100:.2f}",
        total_len_mm=f"{result['total_len_mm']:.1f}",
        est_extrusion_mm=f"{result['est_extrusion_mm']:.2f}",
        area_w_mm=f"{result['area_w_mm']:.1f}",
        area_h_mm=f"{result['area_h_mm']:.1f}",
        working_resolution=f"{result['working_resolution'][0]}x{result['working_resolution'][1]}",
        scale_pct=result["scale_pct"],
        offset_x_mm=result["offset_x_mm"],
        offset_y_mm=result["offset_y_mm"],
        print_height_mm=f"{result['print_height_mm']:.2f}",
        extrusion_per_mm=f"{result['extrusion_per_mm']:.4f}",
        warnings=warnings,
        **_form_bounds(),
    )


if __name__ == "__main__":
    app.run(debug=False, port=5000)

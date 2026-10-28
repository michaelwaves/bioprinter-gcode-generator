"""Geometry and download regressions. Run: python -m unittest -v."""

import io
import math
import re
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import line_trace_gcode as ltg
from app import app


class DownloadFields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.in_download = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_download = attrs.get("action") == "/download"
        elif tag == "input" and self.in_download:
            self.fields[attrs["name"]] = attrs["value"]

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_download = False


class PrintAreaTests(unittest.TestCase):
    def test_square_presets_keep_full_buffered_corners(self):
        for key, half in (("square_150", 72), ("square_200", 97)):
            with self.subTest(key=key):
                plate = ltg.Plate.from_key(key)
                lo, hi = 100 - half, 100 + half
                perimeter = [[(lo, lo), (hi, lo), (hi, hi), (lo, hi), (lo, lo)]]
                paths, clipped, lost = ltg.clip_paths_to_plate(perimeter, plate)
                self.assertEqual(paths, perimeter)
                self.assertFalse(clipped)
                self.assertAlmostEqual(lost, 0)
                self.assertGreater(math.dist(paths[0][0], (100, 100)), half)

    def test_rectangle_crossings_with_both_endpoints_outside(self):
        for path, expected in (
            ([(-5, 5), (15, 5)], [[(0, 5), (10, 5)]]),
            ([(5, -5), (5, 15)], [[(5, 0), (5, 10)]]),
            ([(-5, -5), (15, 15)], [[(0, 0), (10, 10)]]),
            ([(-5, 0), (15, 0)], [[(0, 0), (10, 0)]]),
            ([(-5, -1), (15, -1)], []),
            ([(11, 1), (11, 9)], []),
            ([], []),
            ([(5, 5)], []),
        ):
            with self.subTest(path=path):
                self.assertEqual(ltg.clip_polyline_to_rectangle(path, 0, 0, 10, 10), expected)

    def test_rectangle_exit_and_reentry_stay_separate(self):
        paths = ltg.clip_polyline_to_rectangle(
            [(5, 2), (15, 2), (15, 8), (5, 8)], 0, 0, 10, 10)
        self.assertEqual(paths, [[(5, 2), (10, 2)], [(10, 8), (5, 8)]])

    def test_square_clipping_reports_lost_length_and_drops_tiny_fragments(self):
        plate = ltg.Plate.from_key("square_200")
        paths, clipped, lost = ltg.clip_paths_to_plate(
            [[(-10, 100), (210, 100)], [(2, 50), (3.1, 50)]], plate)
        self.assertEqual(paths, [[(3, 100), (197, 100)]])
        self.assertTrue(clipped)
        self.assertAlmostEqual(lost, 27.1)

    def test_round_presets_still_use_circular_clipping(self):
        for key in ("petri_85", "petri_100"):
            with self.subTest(key=key):
                plate = ltg.Plate.from_key(key)
                r = plate.print_area_radius_mm
                # A crossing plus a segment at a corner of the bounding square.
                source = [[(0, 100), (200, 100)], [(100 + r, 100 + r), (100 + r - 1, 100 + r)]]
                paths, clipped, lost = ltg.clip_paths_to_plate(source, plate)
                self.assertEqual(paths, [[(100 - r, 100), (100 + r, 100)]])
                self.assertTrue(clipped)
                self.assertAlmostEqual(lost, 201 - 2 * r)

    def test_square_bounds_check_detects_offset_in_short_dimension(self):
        plate = ltg.Plate.from_key("square_200")
        mask = np.ones((50, 200), dtype=bool)
        for offset_y, expected in ((0, True), (73, False)):
            placement = ltg.pixel_center_and_scale(
                mask, 50, 200, return_legacy=False, offset_y_mm=offset_y, plate=plate)
            self.assertEqual(ltg.design_bounds_check(mask, 50, 200, *placement, plate=plate)[0], expected)

    def test_preview_boundary_matches_plate_shape(self):
        for key, tag, span in (("square_200", "rect", "194.000"),
                               ("square_150", "rect", "144.000"),
                               ("petri_100", "circle", None)):
            with self.subTest(key=key):
                svg = ET.fromstring(ltg.build_preview_svg([], ltg.Plate.from_key(key), .003))
                boundary = next(e for e in svg.iter() if e.get("stroke") == "#c0392b")
                self.assertTrue(boundary.tag.endswith("}" + tag))
                if span:
                    self.assertEqual(boundary.get("width"), span)
                    self.assertEqual(boundary.get("height"), span)

    def test_pipeline_emits_square_corners_beyond_old_circle(self):
        perimeter = np.array([(0, 0), (0, 199), (199, 199), (199, 0), (0, 0)] * 4)
        # Control the tracer output to exercise exact plate-edge geometry end to end.
        with patch.object(ltg, "hybrid_paths_from_mask", return_value=[perimeter]):
            result = ltg.process_image(Image.new("L", (200, 200), 255),
                                       plate_key="square_200", write_files=False)
        self.assertFalse(result["was_clipped"])
        self.assertEqual(result["area_w_mm"], 194)
        self.assertIn("X197.000 Y197.000", result["gcode_text"])
        self.assertIn("X3.000 Y3.000", result["gcode_text"])
        self.assertNotIn("G28", result["gcode_text"])

    def test_web_generate_regenerate_and_download_preserve_square_200(self):
        image = Image.new("L", (200, 200), 255)
        ImageDraw.Draw(image).rectangle((5, 5, 194, 194), outline=0, width=3)
        upload = io.BytesIO()
        image.save(upload, format="PNG")
        upload.seek(0)
        with app.test_client() as client:
            self.assertIn(b'value="square_200"', client.get("/").data)
            response = client.post("/generate", data={
                "plate_key": "square_200", "trace_mode": "edge", "image": (upload, "square.png")})
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"194 x 194mm square print area", response.data)
            fields = DownloadFields(response.get_data(as_text=True)).fields
            self.assertEqual(fields["plate_key"], "square_200")
            first_download = client.post("/download", data=fields)
            self.assertIn("square_200.gcode", first_download.headers["Content-Disposition"])
            gcode = first_download.get_data(as_text=True)
            moves = [(float(x), float(y)) for x, y in re.findall(r"X([\d.-]+) Y([\d.-]+)", gcode)]
            self.assertTrue(moves)
            self.assertTrue(all(3 <= x <= 197 and 3 <= y <= 197 for x, y in moves))
            self.assertTrue(any(math.dist(p, (100, 100)) > 97 for p in moves))
            # Regeneration and download must produce the same paths from the carried state.
            regenerated = client.post("/regenerate", data=fields)
            self.assertEqual(regenerated.status_code, 200)
            next_fields = DownloadFields(regenerated.get_data(as_text=True)).fields
            self.assertEqual(first_download.data, client.post("/download", data=next_fields).data)
            fields["scale_pct"] = "200"
            fields["offset_x_mm"] = "40"
            clipped = client.post("/regenerate", data=fields)
            self.assertIn(b"was clipped", clipped.data)
            self.assertIn(b"bounding box extends outside", clipped.data)


if __name__ == "__main__":
    unittest.main()

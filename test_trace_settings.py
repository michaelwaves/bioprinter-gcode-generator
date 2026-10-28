"""Verify detail selection and settings carried through preview/download requests."""

import io
import unittest

import numpy as np
from PIL import Image, ImageDraw

import line_trace_gcode as ltg
from app import app
from test_print_area import DownloadFields


class TraceSettingsTests(unittest.TestCase):
    def test_threshold_selects_grayscale_pixels_and_excludes_white(self):
        image = Image.fromarray(np.array([[0, 100, 128, 180, 255]], dtype=np.uint8))
        for threshold, expected in (
            (0, [False, False, False, False, False]),
            (1, [True, False, False, False, False]),
            (128, [True, True, False, False, False]),
            (200, [True, True, True, True, False]),
            (255, [True, True, True, True, False]),
        ):
            with self.subTest(threshold=threshold):
                _, mask, _, _ = ltg.prepare_image_for_print(image, dark_threshold=threshold)
                self.assertEqual(mask.tolist(), [expected])
        for invalid in (-1, 256, 128.5, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ltg.prepare_image_for_print(image, dark_threshold=invalid)

    def test_square_height_is_exact_and_manual_height_still_applies(self):
        for key in ("square_150", "square_200"):
            with self.subTest(key=key):
                plate = ltg.Plate.from_key(key)
                self.assertEqual(plate.default_print_height_mm(), 3.2)
                self.assertIn("G1 Z3.200 F600", ltg.generate_gcode([], plate=plate))
                self.assertIn("G1 Z4.400 F600", ltg.generate_gcode([], plate=plate, print_height_mm=4.4))
        for key in ("petri_85", "petri_100"):
            plate = ltg.Plate.from_key(key)
            self.assertAlmostEqual(plate.default_print_height_mm(), plate.agar_surface_height_mm() + .1)

    def test_threshold_changes_all_trace_modes_and_survives_download(self):
        image = Image.new("L", (100, 100), 255)
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 35, 80), fill=40)
        draw.rectangle((60, 10, 90, 80), fill=180)
        with app.test_client() as client:
            index = client.get("/").get_data(as_text=True)
            self.assertIn('name="dark_threshold"', index)
            self.assertIn('value="3.20"', index)
            for mode in ("edge", "both", "skeletonize"):
                with self.subTest(mode=mode):
                    upload = io.BytesIO()
                    image.save(upload, format="PNG")
                    upload.seek(0)
                    low = client.post("/generate", data={
                        "image": (upload, "detail.png"), "trace_mode": mode,
                        "plate_key": "square_200", "dark_threshold": "120"})
                    self.assertEqual(low.status_code, 200)
                    fields = DownloadFields(low.get_data(as_text=True)).fields
                    self.assertEqual(fields["dark_threshold"], "120")
                    self.assertEqual(fields["print_height_mm"], "3.20")
                    low_download = client.post("/download", data=fields)
                    self.assertEqual(low_download.status_code, 200)
                    self.assertIn(b"G1 X", low_download.data)
                    self.assertIn(b"G1 Z3.200 F600", low_download.data)
                    fields.update(dark_threshold="220", print_height_mm="4.4")
                    high = client.post("/regenerate", data=fields)
                    self.assertEqual(high.status_code, 200)
                    high_fields = DownloadFields(high.get_data(as_text=True)).fields
                    self.assertEqual(high_fields["dark_threshold"], "220")
                    self.assertEqual(high_fields["print_height_mm"], "4.40")
                    high_download = client.post("/download", data=high_fields)
                    self.assertNotEqual(low_download.data, high_download.data)
                    # Compare identical heights to ensure the threshold changes actual paths.
                    high_fields["print_height_mm"] = "3.20"
                    self.assertNotEqual(low_download.data, client.post("/download", data=high_fields).data)
                    self.assertEqual(high_download.data, client.post("/download", data=high_fields | {
                        "print_height_mm": "4.40"}).data)
            for raw, expected in (("bad", "128"), ("-1", "0"), ("999", "255")):
                with self.subTest(raw=raw):
                    response = client.post("/regenerate", data=fields | {"dark_threshold": raw})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(DownloadFields(response.get_data(as_text=True)).fields["dark_threshold"], expected)


if __name__ == "__main__":
    unittest.main()

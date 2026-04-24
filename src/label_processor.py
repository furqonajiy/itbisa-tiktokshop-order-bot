"""
label_processor.py
------------------
Converts a shipping label PDF into PNG image bytes.

The employee prefers images over PDFs because Telegram handles them well on
mobile and Epson iPrint can print them directly. This module is pure local
computation — no network calls.
"""

import io

from pdf2image import convert_from_bytes

from src import config


def pdf_to_pngs(pdf_bytes):
    """Converts every page of a PDF into PNG bytes, returned as a list."""
    images = convert_from_bytes(pdf_bytes, dpi=config.LABEL_IMAGE_DPI)
    return [_image_to_png_bytes(_crop_bottom_whitespace(image)) for image in images]


def _crop_bottom_whitespace(image, white_threshold=250, bottom_padding_px=8):
    """Removes trailing blank space at the bottom of a rendered label image.

    Top/left/right are kept intact for safety — we only trim the bottom.
    """
    grayscale = image.convert("L")
    width, height = grayscale.size
    pixels = grayscale.load()

    last_content_row = None
    for y in range(height - 1, -1, -1):
        for x in range(width):
            if pixels[x, y] < white_threshold:
                last_content_row = y
                break
        if last_content_row is not None:
            break

    if last_content_row is None:
        return image

    crop_bottom = min(height, last_content_row + 1 + bottom_padding_px)
    if crop_bottom >= height:
        return image

    return image.crop((0, 0, width, crop_bottom))


def _image_to_png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
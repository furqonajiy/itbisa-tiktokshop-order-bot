"""
label_processor.py
------------------
Converts a shipping label PDF into Telegram-ready PNG image bytes.

The employee prefers images over PDFs because Telegram handles them well on
mobile and Epson iPrint can print them directly. This module is pure local
computation — no network calls.

Multi-page behavior:
  - A 1-page PDF becomes 1 image.
  - A 2-page PDF becomes 1 merged image.
  - A 3-page PDF becomes 2 images: pages 1-2 merged, page 3 alone.
  - A 4-page PDF becomes 2 merged images, and so on.
"""

import io

from PIL import Image
from pdf2image import convert_from_bytes

from src import config

_MERGED_PAGE_GAP_PX = 12

# A row counts as real label content only when it holds at least this many dark
# pixels (a fraction of the width, with a small floor). Scanning for a SINGLE
# dark pixel let one sub-visible render speck in the blank area below the label
# defeat the whole crop — the intermittent "waybill not cropped" bug: the scan
# stopped at that speck and kept the entire blank A4 tail. Requiring a short run
# of dark pixels ignores isolated specks while still catching any genuine text
# or barcode row (those have far more dark pixels than the threshold).
_CONTENT_ROW_MIN_DARK_FRACTION = 0.01
_CONTENT_ROW_MIN_DARK_FLOOR = 6

# Genuine label rows (barcodes, the address block, the box frame, the
# order-detail table) always carry ink in the CENTRE of the row. Courier resi
# watermarks are tiled down the page MARGINS, and render specks land in the
# corners -- both sit outside the centre. Counting only within the central band
# means a margin-only row cannot bind the crop and keep the whole blank A4 tail
# attached (the recurring "resi not cropped / long resi" bug).
#
# The sibling Shopee bot got this fix; this one did not, so it kept scanning the
# FULL width and a single watermark row held its tail. Keep the two in step.
_CONTENT_CENTER_BAND_FRACTION = 0.5  # inspect the middle 50% of the width

# A speck is one or two stray pixels; any real printed stroke is wider. At 200
# DPI three pixels is 0.4 mm, comfortably below the thinnest barcode bar, so this
# rejects specks WITHOUT risking a real row. Deliberately small: over-cropping
# cuts label content off, which is worse than leaving a tail attached.
_CONTENT_MIN_RUN_PX = 3


def _content_row_min_dark(width):
    """Minimum dark pixels a row needs to count as content (not a stray speck)."""
    return max(_CONTENT_ROW_MIN_DARK_FLOOR, int(width * _CONTENT_ROW_MIN_DARK_FRACTION))


def _row_is_content(pixels, y, x_start, x_end, white_threshold, min_dark):
    """True when row `y` holds real label ink within [x_start, x_end).

    TWO conditions, because either alone has been fooled in production:
      * total dark pixels >= `min_dark` -- rejects a lone speck;
      * longest CONSECUTIVE dark run >= `_CONTENT_MIN_RUN_PX` -- rejects a
        scattered watermark that clears the total but is only isolated dots.
    The previous code checked the total ONLY, while its comment claimed it
    required a run; a sparse watermark crossing the band therefore passed.
    """
    total = 0
    run = best_run = 0
    for x in range(x_start, x_end):
        if pixels[x, y] < white_threshold:
            total += 1
            run += 1
            if run > best_run:
                best_run = run
        else:
            run = 0
    return total >= min_dark and best_run >= _CONTENT_MIN_RUN_PX


def pdf_to_pngs(pdf_bytes):
    """Converts every PDF page into Telegram-ready PNG images.

    Every 2 PDF pages are merged into 1 image before sending to Telegram.
    This reduces message count for multi-page labels without changing the
    original page order.
    """
    images = convert_from_bytes(pdf_bytes, dpi=config.LABEL_IMAGE_DPI)
    cropped_images = [_crop_bottom_whitespace(image) for image in images]
    merged_images = _merge_pages_every_two(cropped_images)
    return [_image_to_png_bytes(image) for image in merged_images]


def _merge_pages_every_two(images):
    """Groups rendered PDF pages into Telegram images, two pages per image."""
    merged_images = []

    for start_index in range(0, len(images), 2):
        merged_images.append(_merge_page_pair(images[start_index:start_index + 2]))

    return merged_images


def _merge_page_pair(images):
    """Stacks up to two rendered PDF pages into one Telegram image.

    We merge vertically so both labels remain readable on mobile and printable
    from Epson iPrint. Width is centered when the two pages have different
    sizes. A small white gap separates the labels visually.
    """
    if len(images) == 1:
        return images[0]

    width = max(image.width for image in images)
    height = sum(image.height for image in images) + _MERGED_PAGE_GAP_PX
    merged = Image.new("RGB", (width, height), "white")

    y_offset = 0
    for image in images:
        image = image.convert("RGB")
        x_offset = (width - image.width) // 2
        merged.paste(image, (x_offset, y_offset))
        y_offset += image.height + _MERGED_PAGE_GAP_PX

    return merged


def _crop_bottom_whitespace(image, white_threshold=250, bottom_padding_px=8):
    """Removes trailing blank space at the bottom of a rendered label image.

    Top/left/right are kept intact for safety — we only trim the bottom.
    """
    grayscale = image.convert("L")
    width, height = grayscale.size
    pixels = grayscale.load()
    min_dark = _content_row_min_dark(width)

    # Only inspect the central band; the outer margins carry the tiled resi
    # watermark and corner specks, neither of which is label content.
    margin = int(width * (1 - _CONTENT_CENTER_BAND_FRACTION) / 2)
    x_start, x_end = margin, width - margin

    last_content_row = None
    for y in range(height - 1, -1, -1):
        if _row_is_content(pixels, y, x_start, x_end, white_threshold, min_dark):
            last_content_row = y
            break

    if last_content_row is None:
        return image

    crop_bottom = min(height, last_content_row + 1 + bottom_padding_px)
    if crop_bottom >= height:
        # Nothing trimmed. Say so: a silent no-op here is exactly what "the resi
        # is long again" looks like, and without this line the log gives no way
        # to tell a correctly-full page from a crop that failed.
        print(f"  [label] tidak ada yang dipotong — baris konten terakhir "
              f"{last_content_row} dari tinggi {height}")
        return image

    print(f"  [label] dipotong {height - crop_bottom}px dari {height}px "
          f"(konten terakhir di baris {last_content_row})")
    return image.crop((0, 0, width, crop_bottom))


def _image_to_png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

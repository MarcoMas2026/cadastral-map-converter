"""
Vercel Serverless Function - Cadastral / Street Map to Blueprint Converter
Path: /api/process  (file: api/process.py)

Vercel's Python runtime expects a class named `handler` subclassing
http.server.BaseHTTPRequestHandler.

Approach: instead of naive edge detection, the input map is segmented BY COLOUR
(water = blue, parkland = green, buildings = grey blocks, roads = near-white
lines) and redrawn as a clean navy/white blueprint of the community.

Expected POST body (JSON):  { "imageBase64": "<base64 image>" }
"""

from http.server import BaseHTTPRequestHandler
import io
import json
import base64

import numpy as np
from PIL import Image, ImageFilter


CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
}

# ---- Blueprint palette -------------------------------------------------------
LAND_BG       = (13, 41, 75)      # deep navy (land)
SEA           = (7, 22, 45)       # darker navy (water)
COAST         = (120, 178, 240)   # coastline / water edge
BUILDING_FILL = (36, 92, 158)     # building footprints
BUILDING_EDGE = (175, 212, 255)   # building outlines
ROAD          = (243, 249, 255)   # roads
PARK          = (18, 52, 70)       # subtle parkland tint
MAX_DIM       = 1600              # downscale very large uploads for speed


class handler(BaseHTTPRequestHandler):
    def _send(self, status_code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        self._send(200, {'status': 'ok', 'message': 'POST an imageBase64 to convert.'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b''
            body = json.loads(raw.decode('utf-8')) if raw else {}
            image_base64 = body.get('imageBase64')
            if not image_base64:
                return self._send(400, {'success': False, 'error': 'No imageBase64 provided'})

            try:
                image = Image.open(io.BytesIO(base64.b64decode(image_base64))).convert('RGB')
            except Exception as e:
                return self._send(400, {'success': False, 'error': f'Invalid image data: {e}'})

            png_b64, svg_b64, w, h = make_blueprint(image)
            return self._send(200, {
                'success': True,
                'pngBase64': png_b64,
                'svgBase64': svg_b64,
                'message': 'Blueprint generated successfully',
                'dimensions': {'width': w, 'height': h},
            })
        except Exception as e:
            return self._send(500, {'success': False, 'error': str(e), 'type': type(e).__name__})


# ---- Morphology helpers (PIL, no OpenCV) ------------------------------------
def _mask_img(mask):
    return Image.fromarray((mask.astype(np.uint8) * 255), mode='L')


def dilate(mask, size=3):
    return np.asarray(_mask_img(mask).filter(ImageFilter.MaxFilter(size))) > 127


def erode(mask, size=3):
    return np.asarray(_mask_img(mask).filter(ImageFilter.MinFilter(size))) > 127


def opening(mask, size=3):
    return dilate(erode(mask, size), size)


def closing(mask, size=3):
    return erode(dilate(mask, size), size)


# ---- Core pipeline -----------------------------------------------------------
def segment(arr):
    """Classify each pixel of an RGB int16 array into water / green / road / building."""
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(2)
    mn = arr.min(2)
    sat = mx - mn
    bright = arr.sum(2) // 3

    water = (b - r > 16) & (b - g > 6) & (b > 165)
    green = (g - r > 6) & (g - b > 6) & ~water

    # Estimate the land background brightness (the dominant pale tone).
    landish = (~water) & (~green) & (sat < 26) & (bright > 200)
    bg_bright = float(np.median(bright[landish])) if landish.any() else 235.0

    # Roads: near-white casing, brighter than the beige background.
    road = (mn > 243) & (~water)

    # Buildings: low-saturation grey blocks clearly darker than the background,
    # but not as dark as text labels (which we exclude with the lower bound).
    building = (
        (~water) & (~green) & (~road)
        & (sat < 24)
        & (bright < bg_bright - 9)
        & (bright > 120)
    )
    return water, green, road, building


def make_blueprint(image):
    # Downscale large uploads (keeps the serverless function fast & within memory).
    if max(image.size) > MAX_DIM:
        scale = MAX_DIM / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    arr = np.asarray(image, dtype=np.int16)
    h, w = arr.shape[:2]
    water, green, road, building = segment(arr)

    # Clean up masks.
    building = opening(building, 3)          # drop text specks / thin noise
    building = closing(building, 3)          # fill small gaps inside footprints
    road = closing(road, 3)
    road = dilate(road, 3)                   # make road lines read clearly
    water = closing(water, 5)
    coast = dilate(water, 7) & ~water        # coastline band

    bld_edge = building & ~erode(building, 3)

    # Compose the blueprint.
    out = np.empty((h, w, 3), dtype=np.uint8)
    out[:] = LAND_BG
    out[green] = PARK
    out[water] = SEA
    out[coast] = COAST
    out[building] = BUILDING_FILL
    out[bld_edge] = BUILDING_EDGE
    out[road] = ROAD

    blueprint = Image.fromarray(out, mode='RGB').filter(ImageFilter.SMOOTH)

    png_buf = io.BytesIO()
    blueprint.save(png_buf, format='PNG', optimize=True)
    png_b64 = base64.b64encode(png_buf.getvalue()).decode('utf-8')

    svg_b64 = base64.b64encode(_svg_wrap(png_b64, w, h).encode('utf-8')).decode('utf-8')
    return png_b64, svg_b64, w, h


def _svg_wrap(png_b64, w, h):
    """Scalable SVG container embedding the rendered blueprint raster."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {w} {h}" width="{w}" height="{h}">\n'
        f'  <image width="{w}" height="{h}" '
        f'xlink:href="data:image/png;base64,{png_b64}"/>\n'
        '</svg>'
    )

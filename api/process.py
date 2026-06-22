"""
Vercel Serverless Function - Cadastral Map to Blueprint Converter
Path: /api/process  (file: api/process.py)

Vercel's Python runtime expects a class named `handler` that subclasses
http.server.BaseHTTPRequestHandler. Each HTTP method is implemented as
do_GET / do_POST / do_OPTIONS.

Expected POST body (JSON):
    { "imageBase64": "<base64-encoded-image-string>" }
"""

from http.server import BaseHTTPRequestHandler
import io
import json
import base64

import cv2
import numpy as np
from PIL import Image, ImageFilter


CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
}


class handler(BaseHTTPRequestHandler):
    def _send(self, status_code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        for key, value in CORS_HEADERS.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        for key, value in CORS_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()

    def do_GET(self):
        # Simple health check so visiting /api/process in a browser works.
        self._send(200, {'status': 'ok', 'message': 'POST an imageBase64 to convert.'})

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(content_length) if content_length else b''
            body = json.loads(raw.decode('utf-8')) if raw else {}
            image_base64 = body.get('imageBase64')

            if not image_base64:
                return self._send(400, {'success': False, 'error': 'No imageBase64 provided'})

            try:
                image_data = base64.b64decode(image_base64)
                image = Image.open(io.BytesIO(image_data)).convert('RGB')
            except Exception as e:
                return self._send(400, {'success': False, 'error': f'Invalid image data: {str(e)}'})

            png_base64, svg_base64, width, height = process_image(image)

            return self._send(200, {
                'success': True,
                'pngBase64': png_base64,
                'svgBase64': svg_base64,
                'message': 'Blueprint generated successfully',
                'dimensions': {'width': width, 'height': height},
            })

        except Exception as e:
            return self._send(500, {
                'success': False,
                'error': str(e),
                'type': type(e).__name__,
            })


def process_image(image):
    """Convert a PIL RGB image into a blueprint PNG + SVG. Returns base64 strings + dims."""
    image_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    # Step 1: Edge detection
    gray = cv2.cvtColor(image_cv, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)

    # Make lines more prominent
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(edges, kernel, iterations=1)

    height, width = dilated.shape

    # Step 2 & 3: blueprint background (dark blue) with white edges (vectorized)
    blueprint_arr = np.empty((height, width, 3), dtype=np.uint8)
    blueprint_arr[:] = (15, 50, 100)
    blueprint_arr[dilated > 128] = (255, 255, 255)
    blueprint = Image.fromarray(blueprint_arr, mode='RGB')

    # Optional subtle glow
    blueprint = blueprint.filter(ImageFilter.GaussianBlur(radius=0.5))

    # Step 4: PNG -> base64
    png_buffer = io.BytesIO()
    blueprint.save(png_buffer, format='PNG')
    png_base64 = base64.b64encode(png_buffer.getvalue()).decode('utf-8')

    # Step 5: SVG -> base64
    svg_content = create_svg_from_image(dilated, width, height)
    svg_base64 = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')

    return png_base64, svg_base64, width, height


def create_svg_from_image(image_array, width, height):
    """Create a simple SVG representation of the detected edges."""
    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        f'  <rect width="{width}" height="{height}" fill="#0f3264"/>',
    ]

    contours, _ = cv2.findContours(image_array, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    for contour in contours:
        if cv2.contourArea(contour) > 100:  # filter small noise
            epsilon = 0.02 * cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, epsilon, True)

            path_data = []
            for i, point in enumerate(approx):
                x, y = point[0]
                path_data.append(f"{'M' if i == 0 else 'L'} {x} {y}")
            if path_data:
                path_data.append("Z")
                d_attr = " ".join(path_data)
                svg_lines.append(
                    f'  <path d="{d_attr}" stroke="#ffffff" stroke-width="1" fill="none" '
                    f'stroke-linecap="round" stroke-linejoin="round"/>'
                )

    svg_lines.append('</svg>')
    return '\n'.join(svg_lines)

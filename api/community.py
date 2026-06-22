"""
Vercel Serverless Function - Community -> Blueprint (OpenStreetMap vectors)
Path: /api/community  (file: api/community.py)

Given a community name (or address, or lat/lon, or an explicit bbox) this
geocodes the place, pulls the real vector geometry from OpenStreetMap via the
Overpass API (building footprints, roads, coastline, swimming pools, tennis
pitches, beaches, named amenities) and returns a LAYERED SVG.

Each feature class is its own <g id="..."> group with a sensible default
colour, so a designer can recolour / hide layers in Illustrator to match the
hand-drawn community-map template. Output is true vector geometry, not a trace.

POST body (JSON), any one of:
    { "query": "Cala Comtesa Illetes Calvia", "radius": 350 }
    { "lat": 39.5346, "lon": 2.5893, "radius": 350 }
    { "bbox": [south, west, north, east] }
"""

from http.server import BaseHTTPRequestHandler
import json
import math
import urllib.parse
import urllib.request

UA = 'cadastral-map-converter/1.0 (real-estate landing map tool)'

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
}

OVERPASS_ENDPOINTS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
]

# Background of the whole drawing.
BACKGROUND = '#ffffff'

# Default layer styling (geometry only — designer restyles per <g> layer).
# White background: geometry is dark navy / blue line-work, amenities keep colour.
LAYER_STYLE = {
    'coastline': {'stroke': '#2b6cb0', 'fill': 'none', 'width': 2},
    'beach':     {'stroke': '#d9a521', 'fill': 'none', 'width': 5},
    'water':     {'stroke': '#7fb2e6', 'fill': 'none', 'width': 1},
    'roads':     {'stroke': '#102945', 'fill': 'none', 'width': 2},
    'paths':     {'stroke': '#6b7c93', 'fill': 'none', 'width': 1.2, 'dash': '5 5'},
    'buildings': {'stroke': '#102945', 'fill': 'none', 'width': 1.2},
    'pools':     {'stroke': '#1a90b0', 'fill': '#34c0e0', 'width': 1.2},
    'tennis':    {'stroke': '#e6701a', 'fill': 'none', 'width': 2},
    'pitch':     {'stroke': '#3f9d52', 'fill': 'none', 'width': 2},
    'amenities': {'stroke': '#c8881e', 'fill': 'none', 'width': 1.4},
}

PATH_HIGHWAYS = {'footway', 'path', 'steps', 'track', 'cycleway', 'pedestrian'}


class handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
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
        self._send(200, {'status': 'ok', 'message': 'POST {query|lat/lon|bbox, radius} to build a community blueprint.'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}

            radius = float(body.get('radius') or 350)
            center = None

            if body.get('bbox'):
                s, w, n, e = [float(x) for x in body['bbox']]
            else:
                if body.get('lat') is not None and body.get('lon') is not None:
                    lat, lon = float(body['lat']), float(body['lon'])
                    label = body.get('query') or f'{lat:.5f}, {lon:.5f}'
                elif body.get('query'):
                    geo = geocode(body['query'])
                    if not geo:
                        return self._send(404, {'success': False, 'error': f"Could not find '{body['query']}'. Try adding town + region, or use lat/lon."})
                    lat, lon, label = geo
                else:
                    return self._send(400, {'success': False, 'error': 'Provide query, lat/lon, or bbox.'})
                center = {'lat': lat, 'lon': lon, 'label': label}
                s, w, n, e = bbox_from_point(lat, lon, radius)

            elements = overpass(s, w, n, e)
            svg, counts = build_svg(elements, s, w, n, e)

            return self._send(200, {
                'success': True,
                'svg': svg,
                'counts': counts,
                'bbox': [s, w, n, e],
                'center': center,
                'message': 'Community blueprint generated from OpenStreetMap.',
            })
        except urllib.error.URLError as e:
            self._send(504, {'success': False, 'error': f'Upstream map service unavailable: {e}. Try again.'})
        except Exception as e:
            self._send(500, {'success': False, 'error': str(e), 'type': type(e).__name__})


# ---- Geocoding (Nominatim) ---------------------------------------------------
def geocode(query):
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
        {'q': query, 'format': 'json', 'limit': 1})
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode('utf-8'))
    if not data:
        return None
    top = data[0]
    return float(top['lat']), float(top['lon']), top.get('display_name', query)


def bbox_from_point(lat, lon, radius_m):
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * math.cos(math.radians(lat)) or 1e-6)
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


# ---- Overpass ----------------------------------------------------------------
def overpass(s, w, n, e):
    bbox = f'{s},{w},{n},{e}'
    q = f"""
[out:json][timeout:50];
(
  way["building"]({bbox});
  way["highway"]({bbox});
  way["leisure"]({bbox});
  way["sport"]({bbox});
  way["natural"~"coastline|beach|water"]({bbox});
  way["amenity"]({bbox});
  node["amenity"]({bbox});
  node["leisure"]({bbox});
);
out geom;
""".strip()
    last_err = None
    for ep in OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(ep, data=q.encode('utf-8'),
                                         headers={'User-Agent': UA, 'Content-Type': 'text/plain'})
            with urllib.request.urlopen(req, timeout=55) as r:
                return json.loads(r.read().decode('utf-8')).get('elements', [])
        except Exception as ex:
            last_err = ex
            continue
    raise urllib.error.URLError(f'all Overpass mirrors failed ({last_err})')


# ---- Classification & SVG ----------------------------------------------------
def classify(tags):
    if not tags:
        return None
    if tags.get('leisure') == 'swimming_pool':
        return 'pools'
    if tags.get('sport') == 'tennis' or tags.get('leisure') == 'pitch' and tags.get('sport') == 'tennis':
        return 'tennis'
    if tags.get('leisure') == 'pitch':
        return 'pitch'
    if tags.get('natural') == 'coastline':
        return 'coastline'
    if tags.get('natural') == 'beach':
        return 'beach'
    if tags.get('natural') == 'water':
        return 'water'
    if 'building' in tags:
        return 'buildings'
    if tags.get('highway') in PATH_HIGHWAYS:
        return 'paths'
    if tags.get('highway'):
        return 'roads'
    if tags.get('amenity') or tags.get('leisure'):
        return 'amenities'
    return None


LAYER_ORDER = ['water', 'coastline', 'beach', 'roads', 'paths',
               'buildings', 'pitch', 'tennis', 'pools', 'amenities']


def focus_bbox(elements, s, w, n, e):
    """Crop to the built community: the extent of buildings/pools/courts, not
    the whole square query area (which pulls in open sea and neighbours)."""
    lons, lats = [], []
    for el in elements:
        if classify(el.get('tags', {})) in ('buildings', 'pools', 'tennis', 'pitch'):
            for g in el.get('geometry', []) or []:
                if 'lon' in g:
                    lons.append(g['lon']); lats.append(g['lat'])
    if len(lons) < 2:
        return s, w, n, e
    fw, fe = min(lons), max(lons)
    fs, fn = min(lats), max(lats)
    mx = (fe - fw) * 0.08 or 1e-4
    my = (fn - fs) * 0.08 or 1e-4
    return fs - my, fw - mx, fn + my, fe + mx


def build_svg(elements, s, w, n, e):
    # Tighten the drawing window to the actual community footprint.
    s, w, n, e = focus_bbox(elements, s, w, n, e)
    lat0 = (s + n) / 2.0
    cos0 = math.cos(math.radians(lat0)) or 1e-6
    target_w = 1600.0
    span_x = (e - w) * cos0
    span_y = (n - s)
    scale = (target_w - 80) / span_x if span_x else 1.0
    W = target_w
    H = span_y * scale + 80

    def XY(lon, lat):
        x = (lon - w) * cos0 * scale + 40
        y = (n - lat) * scale + 40
        return f'{x:.1f},{y:.1f}'

    layers = {k: [] for k in LAYER_ORDER}
    counts = {}

    for el in elements:
        layer = classify(el.get('tags', {}))
        if not layer:
            continue
        counts[layer] = counts.get(layer, 0) + 1
        name = (el.get('tags', {}).get('name') or '').replace('"', '')
        title = f'<title>{escape(name)}</title>' if name else ''

        if el.get('type') == 'node' and 'lat' in el:
            cx, cy = XY(el['lon'], el['lat']).split(',')
            layers[layer].append(f'<circle cx="{cx}" cy="{cy}" r="5">{title}</circle>')
            continue

        geom = el.get('geometry') or []
        if len(geom) < 2:
            continue
        pts = ' '.join(XY(g['lon'], g['lat']) for g in geom if 'lon' in g)
        closed = layer in ('buildings', 'pools', 'tennis', 'pitch', 'water')
        tag = 'polygon' if closed else 'polyline'
        layers[layer].append(f'<{tag} points="{pts}">{title}</{tag}>')

    parts = [
        f'<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
        f'width="{W:.0f}" height="{H:.0f}" font-family="sans-serif">',
        f'<rect width="{W:.0f}" height="{H:.0f}" fill="{BACKGROUND}"/>',
    ]
    for layer in LAYER_ORDER:
        if not layers[layer]:
            continue
        st = LAYER_STYLE[layer]
        dash = f' stroke-dasharray="{st["dash"]}"' if st.get('dash') else ''
        parts.append(
            f'<g id="{layer}" fill="{st["fill"]}" stroke="{st["stroke"]}" '
            f'stroke-width="{st["width"]}" stroke-linejoin="round" '
            f'stroke-linecap="round"{dash}>')
        parts.extend(layers[layer])
        parts.append('</g>')
    parts.append('</svg>')
    return '\n'.join(parts), counts


def escape(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;'))

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
    'boundary':  {'stroke': '#102945', 'fill': 'none', 'width': 2.5, 'dash': '10 7'},
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
            boundary = None

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
                    lat, lon, label, boundary = geo
                else:
                    return self._send(400, {'success': False, 'error': 'Provide query, lat/lon, or bbox.'})
                center = {'lat': lat, 'lon': lon, 'label': label}
                # Allow opting out of boundary clipping.
                if body.get('clip') is False:
                    boundary = None
                if boundary and len(boundary) >= 4:
                    bs, bw, bn, be = ring_bbox(boundary)
                    mlat = (bn - bs) * 0.06 or 1e-4
                    mlon = (be - bw) * 0.06 or 1e-4
                    s, w, n, e = bs - mlat, bw - mlon, bn + mlat, be + mlon
                else:
                    boundary = None
                    s, w, n, e = bbox_from_point(lat, lon, radius)

            elements = overpass(s, w, n, e)
            svg, counts = build_svg(elements, s, w, n, e, boundary)

            return self._send(200, {
                'success': True,
                'svg': svg,
                'counts': counts,
                'bbox': [s, w, n, e],
                'center': center,
                'clipped': bool(boundary),
                'message': ('Traced within the community boundary from OpenStreetMap.'
                            if boundary else
                            'No community boundary in OpenStreetMap — showing the area by radius. '
                            'Draw a boundary in the editor to trace only the community.'),
            })
        except urllib.error.URLError as e:
            self._send(504, {'success': False, 'error': f'Upstream map service unavailable: {e}. Try again.'})
        except Exception as e:
            self._send(500, {'success': False, 'error': str(e), 'type': type(e).__name__})


# ---- Geocoding (Nominatim) ---------------------------------------------------
def geocode(query):
    """Returns (lat, lon, label, boundary) where boundary is the community
    outline as a list of (lon,lat) vertices if Nominatim has a polygon for it
    (e.g. a named landuse=residential area), else None."""
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
        {'q': query, 'format': 'json', 'polygon_geojson': 1, 'limit': 1})
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode('utf-8'))
    if not data:
        return None
    top = data[0]
    return (float(top['lat']), float(top['lon']),
            top.get('display_name', query), _ring_from_geojson(top.get('geojson')))


def _ring_from_geojson(gj):
    """Extract the largest outer ring as [(lon,lat),...] from a (Multi)Polygon."""
    if not gj:
        return None
    t = gj.get('type'); c = gj.get('coordinates')
    if t == 'Polygon':
        return [(p[0], p[1]) for p in c[0]]
    if t == 'MultiPolygon':
        best = max(c, key=lambda poly: len(poly[0]))
        return [(p[0], p[1]) for p in best[0]]
    return None


def ring_bbox(ring):
    lons = [p[0] for p in ring]; lats = [p[1] for p in ring]
    return min(lats), min(lons), max(lats), max(lons)


def point_in_ring(lon, lat, ring):
    """Ray-casting point-in-polygon test."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


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


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(math.floor(k)); hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def focus_bbox(elements, s, w, n, e):
    """Crop to the built community core. Uses building *centroids* and trims
    outliers (a lone villa on a far islet) with percentiles so the view is the
    community itself, not the surrounding sea / neighbours."""
    cen_lon, cen_lat = [], []
    for el in elements:
        if classify(el.get('tags', {})) in ('buildings', 'pools', 'tennis', 'pitch'):
            geom = [g for g in (el.get('geometry') or []) if 'lon' in g]
            if not geom:
                continue
            cen_lon.append(sum(g['lon'] for g in geom) / len(geom))
            cen_lat.append(sum(g['lat'] for g in geom) / len(geom))
    if len(cen_lon) < 3:
        return s, w, n, e
    slon, slat = sorted(cen_lon), sorted(cen_lat)
    fw, fe = _pct(slon, 0.03), _pct(slon, 0.97)
    fs, fn = _pct(slat, 0.03), _pct(slat, 0.97)
    mx = (fe - fw) * 0.10 or 1e-4
    my = (fn - fs) * 0.10 or 1e-4
    return fs - my, fw - mx, fn + my, fe + mx


def in_community(el, layer, ring, rbbox):
    """Decide whether an element belongs to the bounded community."""
    if el.get('type') == 'node' and 'lat' in el:
        return point_in_ring(el['lon'], el['lat'], ring)
    geom = [g for g in (el.get('geometry') or []) if 'lon' in g]
    if not geom:
        return False
    if layer in ('buildings', 'pools', 'tennis', 'pitch', 'amenities'):
        clon = sum(g['lon'] for g in geom) / len(geom)
        clat = sum(g['lat'] for g in geom) / len(geom)
        return point_in_ring(clon, clat, ring)
    if layer in ('coastline', 'beach', 'water'):
        # Keep the shoreline that sits within the framed view (coastal communities).
        bs, bw, bn, be = rbbox
        return any(bw <= g['lon'] <= be and bs <= g['lat'] <= bn for g in geom)
    inside = sum(1 for g in geom if point_in_ring(g['lon'], g['lat'], ring))
    return inside >= max(2, len(geom) * 0.35)  # roads/paths: mostly inside


def build_svg(elements, s, w, n, e, boundary=None):
    view_bbox = None
    if boundary and len(boundary) >= 4:
        bs, bw, bn, be = ring_bbox(boundary)
        ml = (bn - bs) * 0.06 or 1e-4
        mo = (be - bw) * 0.06 or 1e-4
        s, w, n, e = bs - ml, bw - mo, bn + ml, be + mo
        view_bbox = (s, w, n, e)              # frame extent, for coast/beach keep
    else:
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
        if boundary and not in_community(el, layer, boundary, view_bbox):
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
        f'<rect id="bp-bg" width="{W:.0f}" height="{H:.0f}" fill="{BACKGROUND}"/>',
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

    if boundary and len(boundary) >= 4:
        pts = ' '.join(XY(lon, lat) for lon, lat in boundary)
        st = LAYER_STYLE['boundary']
        parts.append(
            f'<g id="boundary" fill="none" stroke="{st["stroke"]}" '
            f'stroke-width="{st["width"]}" stroke-dasharray="{st["dash"]}" '
            f'stroke-linejoin="round"><polygon points="{pts}"/></g>')

    parts.append('</svg>')
    return '\n'.join(parts), counts


def escape(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;'))

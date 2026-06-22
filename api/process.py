"""
Vercel Serverless Function - Cadastral Map to Blueprint Converter
Path: /api/process.py

This function runs on Vercel and processes cadastral maps into blueprints.
"""

import cv2
import numpy as np
from PIL import Image, ImageFilter
import io
import base64
import json
from typing import Dict

def handler(request):
    """
    Main handler for Vercel serverless function
    
    Expected POST request body:
    {
        "imageBase64": "<base64-encoded-image-string>"
    }
    """
    
    # Handle CORS
    if request.method == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type'
            }
        }
    
    try:
        # Parse request
        if request.method != 'POST':
            return create_response(400, {'error': 'Only POST requests accepted'})
        
        body = json.loads(request.body) if isinstance(request.body, str) else request.body
        image_base64 = body.get('imageBase64')
        
        if not image_base64:
            return create_response(400, {'error': 'No imageBase64 provided'})
        
        # Decode base64 image
        try:
            image_data = base64.b64decode(image_base64)
            image = Image.open(io.BytesIO(image_data))
        except Exception as e:
            return create_response(400, {'error': f'Invalid image data: {str(e)}'})
        
        # Convert to OpenCV format
        image_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        
        # Step 1: Edge Detection
        gray = cv2.cvtColor(image_cv, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        
        # Dilate to make lines more prominent
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(edges, kernel, iterations=1)
        
        # Step 2: Create blueprint background (dark blue)
        height, width = dilated.shape
        blueprint = Image.new('RGB', (width, height), color=(15, 50, 100))
        
        # Step 3: Create white lines layer
        white_layer = Image.new('RGB', (width, height), color=(0, 0, 0))
        white_pixels = white_layer.load()
        edges_pil = Image.fromarray(dilated)
        edges_pixels = edges_pil.load()
        
        # Apply white to detected edges
        for y in range(height):
            for x in range(width):
                if edges_pixels[x, y] > 128:
                    white_pixels[x, y] = (255, 255, 255)
        
        # Composite white layer onto blueprint
        blueprint.paste(white_layer, (0, 0), white_layer)
        
        # Optional: subtle glow effect
        blueprint = blueprint.filter(ImageFilter.GaussianBlur(radius=0.5))
        
        # Step 4: Convert to base64 PNG
        png_buffer = io.BytesIO()
        blueprint.save(png_buffer, format='PNG')
        png_base64 = base64.b64encode(png_buffer.getvalue()).decode('utf-8')
        
        # Step 5: Create SVG (simple version)
        svg_content = create_svg_from_image(dilated, width, height)
        svg_base64 = base64.b64encode(svg_content.encode('utf-8')).decode('utf-8')
        
        # Return success response
        return create_response(200, {
            'success': True,
            'pngBase64': png_base64,
            'svgBase64': svg_base64,
            'message': 'Blueprint generated successfully',
            'dimensions': {
                'width': width,
                'height': height
            }
        })
    
    except Exception as e:
        return create_response(500, {
            'success': False,
            'error': str(e),
            'type': type(e).__name__
        })


def create_response(status_code: int, body: Dict) -> Dict:
    """
    Create a Vercel-compatible response
    """
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        },
        'body': json.dumps(body)
    }


def create_svg_from_image(image_array: np.ndarray, width: int, height: int) -> str:
    """
    Create a simple SVG representation of the detected edges
    """
    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        f'  <rect width="{width}" height="{height}" fill="#0f3264"/>',
    ]
    
    # Find contours in the edge-detected image
    contours, _ = cv2.findContours(image_array, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    
    # Add contours to SVG
    for contour in contours:
        if cv2.contourArea(contour) > 100:  # Filter small noise
            epsilon = 0.02 * cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, epsilon, True)
            
            path_data = []
            for i, point in enumerate(approx):
                x, y = point[0]
                if i == 0:
                    path_data.append(f"M {x} {y}")
                else:
                    path_data.append(f"L {x} {y}")
            path_data.append("Z")
            
            if path_data:
                d_attr = " ".join(path_data)
                svg_lines.append(
                    f'  <path d="{d_attr}" stroke="#ffffff" stroke-width="1" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
                )
    
    svg_lines.append('</svg>')
    return '\n'.join(svg_lines)

# bookings/utils/distance_utils.py
"""
This module provides utility functions for calculating distance and time matrices using Google Routes API or fallback methods.
- Prioritizes Routes API for accurate driving times/distances with traffic.
- Falls back to geopy great-circle distance with average speed estimate if API fails or for misses.
- Handles hub as depot (index 0 in matrices).
- Key assumptions: Locations are Address objects with latitude/longitude as floats.
- Senior notes: Added type checks for hub_lat/lng to handle potential list inputs (e.g., from query bugs); logs warnings.
"""

import requests
from django.conf import settings
from geopy.distance import great_circle
import logging
from typing import List, Tuple, Optional
import json

logger = logging.getLogger(__name__)

# Routes API limits: 25 origins x 25 destinations per request
API_CHUNK_SIZE = 25


def get_time_matrix(
    locations: List, hub_lat: Optional[float] = None, hub_lng: Optional[float] = None
) -> Tuple[List[List[int]], List[List[float]]]:
    """
    Computes time (seconds) and distance (km) matrices with hub as depot (index 0).
    
    - Uses new Routes API for driving routes with traffic (preferred for accuracy).
    - Falls back to great-circle + min values if API fails.
    - locations: List of Address objects (with latitude/longitude as floats).
    - hub_lat, hub_lng: Hub coordinates (floats; optional but recommended for depot).
    
    Returns: (time_matrix, distance_matrix), both n x n lists (n = 1 + len(valid locations)).
    
    Senior notes:
    - Added type coercion/checks for hub_lat/lng to handle edge cases (e.g., if passed as list from caller bug).
    - If hub_lat/lng is list, takes first element and logs warning – fix caller if persistent.
    - Chunked API calls to respect limits; fieldmask for efficiency.
    - Fallback enforces min time/distance to avoid zero-cost arcs in OR-Tools.
    """
    if not locations and (hub_lat is None or hub_lng is None):
        logger.warning("No locations or hub coords – returning zero matrices")
        return [[0]], [[0.0]]

    # Type coercion/safety for hub coords (handle if accidentally list or str)
    try:
        if isinstance(hub_lat, list):
            logger.warning(f"hub_lat is list: {hub_lat} – taking first element")
            hub_lat = float(hub_lat[0]) if hub_lat else None
        else:
            hub_lat = float(hub_lat) if hub_lat is not None else None

        if isinstance(hub_lng, list):
            logger.warning(f"hub_lng is list: {hub_lng} – taking first element")
            hub_lng = float(hub_lng[0]) if hub_lng else None
        else:
            hub_lng = float(hub_lng) if hub_lng is not None else None
    except (TypeError, ValueError) as e:
        logger.error(f"Invalid hub coords: hub_lat={hub_lat}, hub_lng={hub_lng} – {e}. Setting to None.")
        hub_lat = None
        hub_lng = None

    # Build coords as list of dicts: hub first, then valid locations
    coords = []
    if hub_lat is not None and hub_lng is not None:
        coords.append({"latitude": hub_lat, "longitude": hub_lng})

    valid_locations = [
        loc
        for loc in locations
        if hasattr(loc, "latitude")
        and loc.latitude is not None
        and hasattr(loc, "longitude")
        and loc.longitude is not None
    ]
    for loc in valid_locations:
        coords.append({"latitude": float(loc.latitude), "longitude": float(loc.longitude)})

    n = len(coords)
    if n <= 1:
        logger.debug(f"Only {n} coords – returning zero matrices")
        return [[0] * n for _ in range(n)], [[0.0] * n for _ in range(n)]

    # Initialize empty matrices
    time_matrix: List[List[int]] = [[0] * n for _ in range(n)]
    distance_matrix: List[List[float]] = [[0.0] * n for _ in range(n)]

    def call_api(origins: List[dict], destinations: List[dict]) -> Optional[List[dict]]:
        """
        Makes a single Routes API call for a chunk.
        - Uses POST to computeRouteMatrix.
        - Returns list of matrix elements or None on failure.
        """
        url = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"  # Correct endpoint (updated from v2 to avoid 404; confirm Google Docs if changed)
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition,status"  # Efficient: only needed fields
        }
        body = {
            "origins": [{"waypoint": {"location": {"latLng": o}}} for o in origins],
            "destinations": [{"waypoint": {"location": {"latLng": d}}} for d in destinations],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",  # Pessimistic traffic model
            "languageCode": "en-US",
            "units": "METRIC"
        }
        try:
            response = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Routes API success: {len(data)} elements")
            return data
        except requests.RequestException as e:
            resp_text = getattr(response, 'text', 'No response available') if 'response' in locals() else 'No response (request never completed)'
            logger.error(f"Routes API failed: {e} – Response: {resp_text}")
            return None

    api_success = False
    # Chunked calls: Iterate over origins/destinations in blocks
    for i_start in range(0, n, API_CHUNK_SIZE):
        i_end = min(i_start + API_CHUNK_SIZE, n)
        origins_chunk = coords[i_start:i_end]

        for j_start in range(0, n, API_CHUNK_SIZE):
            j_end = min(j_start + API_CHUNK_SIZE, n)
            destinations_chunk = coords[j_start:j_end]

            data = call_api(origins_chunk, destinations_chunk)
            if data:
                api_success = True
                for elem in data:
                    if elem.get("condition") != "ROUTE_EXISTS":
                        logger.debug(f"Skipped element (condition: {elem.get('condition')}) at ({elem.get('originIndex')}, {elem.get('destinationIndex')})")
                        continue
                    if elem.get("status"):  # Non-empty status = error
                        logger.warning(f"Element error: {elem['status']}")
                        continue

                    ii = i_start + elem.get("originIndex", 0)
                    jj = j_start + elem.get("destinationIndex", 0)

                    duration = elem.get("duration")  # "1234s"
                    if duration:
                        time_matrix[ii][jj] = int(duration.rstrip("s") or 0)

                    dist_m = elem.get("distanceMeters")
                    if dist_m:
                        distance_matrix[ii][jj] = dist_m / 1000.0  # to km

    # If all API calls failed, log and force full fallback
    if not api_success:
        logger.warning("All Routes API calls failed – using full great-circle fallback")

    # Fallback for any zeros/misses: great-circle + mins
    MIN_TIME_SEC = 300  # Min 5 min per arc (avoids zero-cost issues in OR-Tools)
    AVG_SPEED_KMH = 50.0  # Conservative urban speed
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if time_matrix[i][j] == 0 or distance_matrix[i][j] == 0.0:
                dist_km = great_circle(
                    (coords[i]["latitude"], coords[i]["longitude"]),
                    (coords[j]["latitude"], coords[j]["longitude"]),
                ).km
                distance_matrix[i][j] = max(round(dist_km, 3), 0.1)  # Min 0.1 km
                time_matrix[i][j] = max(int((dist_km / AVG_SPEED_KMH) * 3600), MIN_TIME_SEC)

    # Debug samples
    logger.info(f"Time matrix sample (first row): {time_matrix[0]}")
    logger.info(f"Distance matrix sample (first row): {distance_matrix[0]}")

    return time_matrix, distance_matrix


def distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Simple great-circle distance (km) for quick proximity checks.
    - Use get_time_matrix for full routing accuracy.
    - Senior notes: No changes needed; kept for legacy/ sorting use.
    """
    return great_circle((lat1, lng1), (lat2, lng2)).km
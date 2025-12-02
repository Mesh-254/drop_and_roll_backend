import requests
from django.conf import settings
from geopy.distance import great_circle
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

API_CHUNK_SIZE = 10  # Google limit: ~100 elements/request (10x10)

def get_time_matrix(
    locations: List, 
    hub_lat: Optional[float] = None, 
    hub_lng: Optional[float] = None
) -> Tuple[List[List[int]], List[List[float]]]:
    """
    Computes time (seconds) and distance (km) matrices with hub as depot (index 0).
    
    - Prioritizes Google Distance Matrix API for max accuracy (driving routes with traffic).
    - Falls back to great-circle distance + conservative speed estimate if API fails.
    - locations: List of Address objects (e.g., pickups or dropoffs).
    - hub_lat, hub_lng: Hub coordinates (required for depot-based routing).
    
    Returns: (time_matrix, distance_matrix), both n x n where n = 1 (hub) + len(locations).
    """
    if not locations and (hub_lat is None or hub_lng is None):
        return [[0]], [[0.0]]

    # Build coords: hub first (depot), then valid locations
    coords = []
    if hub_lat is not None and hub_lng is not None:
        coords.append(f"{float(hub_lat)},{float(hub_lng)}")

    for loc in locations:
        if hasattr(loc, 'latitude') and loc.latitude is not None and hasattr(loc, 'longitude') and loc.longitude is not None:
            coords.append(f"{float(loc.latitude)},{float(loc.longitude)}")

    n = len(coords)
    if n <= 1:
        return [[0]], [[0.0]]

    # Initialize matrices
    time_matrix: List[List[int]] = [[0] * n for _ in range(n)]
    distance_matrix: List[List[float]] = [[0.0] * n for _ in range(n)]

    def call_api(orig_chunk: List[str], dest_chunk: List[str]) -> Optional[dict]:
        """Makes a single chunked API call."""
        params = {
            'origins': '|'.join(orig_chunk),
            'destinations': '|'.join(dest_chunk),
            'mode': 'driving',
            'departure_time': 'now',
            'traffic_model': 'pessimistic',  # Conservative for max safety/accuracy in estimates
            'key': settings.GOOGLE_MAPS_API_KEY,
        }
        try:
            response = requests.get(
                'https://maps.googleapis.com/maps/api/distancematrix/json',
                params=params,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            if data.get('status') != 'OK':
                logger.warning(f"API status not OK: {data.get('status')} - {data.get('error_message')}")
                return None
            return data
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None

    # Chunked API calls
    for i_start in range(0, n, API_CHUNK_SIZE):
        i_end = min(i_start + API_CHUNK_SIZE, n)
        orig_chunk = coords[i_start:i_end]

        for j_start in range(0, n, API_CHUNK_SIZE):
            j_end = min(j_start + API_CHUNK_SIZE, n)
            dest_chunk = coords[j_start:j_end]

            data = call_api(orig_chunk, dest_chunk)
            if not data:
                continue

            rows = data.get('rows', [])
            for ii, row in enumerate(rows):
                elements = row.get('elements', [])
                for jj, elem in enumerate(elements):
                    status = elem.get('status')
                    if status != 'OK':
                        if status != 'ZERO_RESULTS':
                            logger.debug(f"Element skipped (status: {status}) at ({i_start + ii}, {j_start + jj})")
                        continue

                    i = i_start + ii
                    j = j_start + jj

                    # Time: Prefer traffic-aware, fallback to base duration
                    duration_key = 'duration_in_traffic' if 'duration_in_traffic' in elem else 'duration'
                    duration = elem.get(duration_key)
                    if duration:
                        time_matrix[i][j] = duration.get('value', 0)

                    # Distance: Routed driving km
                    distance = elem.get('distance')
                    if distance:
                        distance_matrix[i][j] = distance.get('value', 0) / 1000.0  # meters to km

    # Fallback: Fill zeros (API misses) with great-circle + conservative speed
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if time_matrix[i][j] == 0 or distance_matrix[i][j] == 0.0:
                lat1, lng1 = _parse_coord(coords[i])
                lat2, lng2 = _parse_coord(coords[j])
                if lat1 is None or lat2 is None:
                    continue
                dist_km = great_circle((lat1, lng1), (lat2, lng2)).km
                distance_matrix[i][j] = round(dist_km, 3)
                time_matrix[i][j] = int(dist_km * 3600 / 50)  # Seconds at 50 km/h (conservative for accuracy)

    return time_matrix, distance_matrix


def _parse_coord(coord_str: str) -> Tuple[Optional[float], Optional[float]]:
    """Parses 'lat,lng' string to (lat, lng) floats, or (None, None) on error."""
    try:
        lat, lng = map(float, coord_str.split(','))
        return lat, lng
    except (ValueError, AttributeError):
        logger.debug(f"Invalid coord: {coord_str}")
        return None, None


def distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculates great-circle distance (km) for proximity sorting. Use get_time_matrix for full accuracy."""
    return great_circle((lat1, lng1), (lat2, lng2)).km
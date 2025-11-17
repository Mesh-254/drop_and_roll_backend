import requests
from django.conf import settings
from geopy.distance import great_circle
import logging

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# CHUNK SIZE (to avoid MAX_ELEMENTS_EXCEEDED – Google limit ~100 elements/request)
# ----------------------------------------------------------------------
API_CHUNK_SIZE = 10  # 10x10 = 100 elements max per call

def get_time_matrix(locations):
    """Batch fetch travel times (seconds) using Google Distance Matrix API with chunking.
    locations: list of objects with latitude/longitude (first is hub).
    Returns: time_matrix [[int]], distance_matrix [[float km]]
    """
    if not locations:
        return [[]], [[]]

    coords = [
        f"{loc.latitude},{loc.longitude}" for loc in locations if loc.latitude is not None]
    n = len(coords)
    if n > 25:
        logger.warning(f"Matrix large ({n}); using chunking.")

    # ------------------------------------------------------------------
    # CHUNKING: Split into sub-matrices and stitch
    # ------------------------------------------------------------------
    def call_api(orig_chunk, dest_chunk):
        origins = '|'.join(orig_chunk)
        destinations = '|'.join(dest_chunk)
        params = {
            'origins': origins,
            'destinations': destinations,
            'mode': 'driving',
            'departure_time': 'now',
            'key': settings.GOOGLE_MAPS_API_KEY,
        }
        url = 'https://maps.googleapis.com/maps/api/distancematrix/json'
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data['status'] != 'OK':
                raise ValueError(f"API error: {data['status']}")
            return data
        except Exception as e:
            logger.error(f"API chunk failed: {e}")
            return None

    time_matrix = [[0] * n for _ in range(n)]
    distance_matrix = [[0.0] * n for _ in range(n)]

    for i_start in range(0, n, API_CHUNK_SIZE):
        orig_chunk = coords[i_start:i_start + API_CHUNK_SIZE]
        for j_start in range(0, n, API_CHUNK_SIZE):
            dest_chunk = coords[j_start:j_start + API_CHUNK_SIZE]
            data = call_api(orig_chunk, dest_chunk)
            if data:
                for ii, row in enumerate(data['rows']):
                    for jj, elem in enumerate(row['elements']):
                        i = i_start + ii
                        j = j_start + jj
                        if 'duration_in_traffic' in elem:
                            time_matrix[i][j] = elem['duration_in_traffic']['value']
                        elif 'duration' in elem:
                            time_matrix[i][j] = elem['duration']['value']
                        if 'distance' in elem:
                            distance_matrix[i][j] = elem['distance']['value'] / 1000.0
            else:
                # Fallback for this chunk
                for ii in range(len(orig_chunk)):
                    for jj in range(len(dest_chunk)):
                        i = i_start + ii
                        j = j_start + jj
                        if i != j:
                            dist_km = great_circle(
                                (locations[i].latitude, locations[i].longitude),
                                (locations[j].latitude, locations[j].longitude)
                            ).km
                            distance_matrix[i][j] = dist_km
                            time_matrix[i][j] = int(dist_km * 3600 / 60)  # 60km/h

    return time_matrix, distance_matrix


def haversine_matrix(locations):
    """Fallback: Distance-based matrix (assume 60km/h speed). Returns time_matrix, distance_matrix"""
    n = len(locations)
    time_matrix = [[0] * n for _ in range(n)]
    distance_matrix = [[0.0] * n for _ in range(n)]  # NEW: Add distance
    for i in range(n):
        for j in range(n):
            if i != j:
                dist_km = great_circle(
                    (locations[i].latitude, locations[i].longitude),
                    (locations[j].latitude, locations[j].longitude)
                ).km
                distance_matrix[i][j] = dist_km  # NEW: Store km
                time_matrix[i][j] = int(dist_km * 3600 / 60)  # Seconds at 60km/h
    return time_matrix, distance_matrix  # MODIFIED: Return both


def distance(lat1, lng1, lat2, lng2):
    """Proximity sort (km)."""
    return great_circle((lat1, lng1), (lat2, lng2)).km
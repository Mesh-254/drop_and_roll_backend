import requests
from django.conf import settings
from geopy.distance import great_circle
import logging

logger = logging.getLogger(__name__)


def get_time_matrix(locations):
    """Batch fetch travel times (seconds) using Google Distance Matrix API.
    locations: list of objects with latitude/longitude (first is hub).
    Returns: time_matrix [[int]], distance_matrix [[float km]]
    """
    if not locations:
        return [[]], [[]]  # MODIFIED: Return both matrices

    coords = [
        f"{loc.latitude},{loc.longitude}" for loc in locations if loc.latitude is not None]
    if len(coords) > 25:
        logger.warning("Matrix too large; truncate or batch further.")
        coords = coords[:25]  # API limit

    origins = '|'.join(coords)
    destinations = origins
    params = {
        'origins': origins,
        'destinations': destinations,
        'mode': 'driving',
        'departure_time': 'now',  # For traffic
        'key': settings.GOOGLE_MAPS_API_KEY,
    }
    url = 'https://maps.googleapis.com/maps/api/distancematrix/json'
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data['status'] != 'OK':
            raise ValueError(f"API error: {data['status']}")

        n = len(coords)
        time_matrix = [[0] * n for _ in range(n)]
        distance_matrix = [[0.0] * n for _ in range(n)]  # NEW: Add distance matrix (km)
        for i, row in enumerate(data['rows']):
            for j, elem in enumerate(row['elements']):
                if 'duration_in_traffic' in elem:
                    time_matrix[i][j] = elem['duration_in_traffic']['value']  # Seconds
                elif 'duration' in elem:
                    time_matrix[i][j] = elem['duration']['value']
                if 'distance' in elem:
                    distance_matrix[i][j] = elem['distance']['value'] / 1000.0  # Meters to km
        return time_matrix, distance_matrix  # MODIFIED: Return both
    except Exception as e:
        logger.error(
            f"Distance Matrix API failed: {e}. Falling back to haversine.")
        return haversine_matrix(locations)  # MODIFIED: Update fallback to return both


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
# bookings/utils/route_optimization.py
"""
This module handles route optimization for bookings using clustering and Vehicle Routing Problem (VRP) solvers.
- Uses KMeans for clustering bookings by location.
- OR-Tools for VRP (multi-vehicle) and TSP (single cluster/route) optimization.
- Supports mixed leg types (pickup + delivery in one route) via stop_types.
- Fallback to clustering if VRP fails, with fresh matrix recomputation per cluster to ensure accurate times/distances.
- Enforces minimum route hours to avoid skips in downstream tasks.
- Key assumptions: Bookings have valid addresses with lat/lng; hub is the depot.
"""

from sklearn.cluster import KMeans
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from django.utils import timezone
from datetime import timedelta
import logging
from driver.models import DriverShift
from django.db import transaction
from django.conf import settings
from bookings.utils.distance_utils import get_time_matrix  # For matrix computation
from typing import (
    List,
    Tuple,
    Optional,
)  # For type hints (senior: improves readability/IDE support)
from django.db.models import QuerySet  # For type check
from driver.models import DriverProfile

logger = logging.getLogger(__name__)

# Force fallback mode from settings (e.g., for testing or if VRP is unreliable)
FORCE_FALLBACK = getattr(settings, "FORCE_FALLBACK", False)


def cluster_bookings(
    bookings, num_clusters=5, hub_lat=None, hub_lng=None, stop_types=None
):
    """
    Clusters bookings based on coordinates, with support for mixed stop types.
    - If stop_types provided (list of 'pickup'/'delivery'), uses corresponding address for coord.
    - Adds hub coords multiple times to bias clusters toward hub.
    - Returns dict of cluster_id: list of (booking, type) tuples for mixed compatibility.
    - Handles invalid (no lat/lng) bookings by distributing them round-robin.
    """
    if not bookings:
        return {}

    # Ensure stop_types is a list matching bookings length; fallback to None (uniform leg_type handled by caller)
    bookings = (
        list(bookings) if isinstance(bookings, QuerySet) else bookings
    )  # Force evaluation to list for consistency
    if not isinstance(stop_types, list) or len(stop_types) != len(bookings):
        logger.warning(
            f"Invalid stop_types type/length: {type(stop_types)}, len={len(stop_types) if hasattr(stop_types, '__len__') else 'N/A'} - resetting to uniform None"
        )
        stop_types = [None] * len(bookings)

    coords = []  # List of [lat, lng] for valid bookings
    valid_bookings = []  # List of (booking, type) tuples for valid ones
    invalid_bookings = []  # List of (booking, type) for those without coords

    for i, b in enumerate(bookings):
        # Select address based on stop_type (mixed mode) or default to pickup then dropoff
        if stop_types[i] == "pickup":
            addr = getattr(b, "pickup_address", None)
        elif stop_types[i] == "delivery":
            addr = getattr(b, "dropoff_address", None)
        else:
            addr = getattr(b, "pickup_address", None) or getattr(
                b, "dropoff_address", None
            )

        if addr and addr.latitude is not None and addr.longitude is not None:
            coords.append([float(addr.latitude), float(addr.longitude)])
            valid_bookings.append((b, stop_types[i]))
        else:
            invalid_bookings.append((b, stop_types[i]))

    # If no valid coords, group all invalid into one cluster
    if not valid_bookings:
        return {0: invalid_bookings}

    original_coords_len = len(coords)

    # Bias toward hub by adding hub coord multiple times (helps pull clusters closer)
    if hub_lat is not None and hub_lng is not None and original_coords_len > 1:
        try:
            hub_lat_f = float(hub_lat)
            hub_lng_f = float(hub_lng)
            hub_coord = [hub_lat_f, hub_lng_f]
            multiples = max(5, original_coords_len // 3)
            coords = [hub_coord] * multiples + coords
        except (TypeError, ValueError) as e:
            logger.warning(
                f"Cannot bias clusters - invalid hub coords: {hub_lat=}, {hub_lng=} → {e}. Continuing without bias."
            )
            # Proceed without adding hub multiples

    # Cap clusters to available valid bookings
    k = min(num_clusters, len(valid_bookings)) or 1
    kmeans = KMeans(n_clusters=k, random_state=0, n_init=10)
    labels = kmeans.fit_predict(coords)

    # Extract labels for bookings only (exclude duplicated hub)
    booking_labels = labels[multiples if "multiples" in locals() else 0 :]

    # Build clusters as dict {cluster_id: [(booking, type), ...]}
    clusters = {i: [] for i in range(k)}
    for label, booking_tuple in zip(booking_labels, valid_bookings):
        clusters[label].append(booking_tuple)

    # Distribute invalid bookings evenly (round-robin)
    for i, b_tuple in enumerate(invalid_bookings):
        clusters[i % k].append(b_tuple)

    return clusters


def optimize_route_single(
    cluster,
    time_matrix,
    distance_matrix,
    driver=None,
    time_windows=None,
    stop_types=None,
    leg_type="pickup",
):
    """
    Optimizes a single cluster into a route using TSP (Traveling Salesman Problem) solver.
    - Handles mixed mode: cluster can be list of bookings or [(booking, 'pickup'/'delivery')] tuples.
    - Normalizes to bookings list and stop_types list internally.
    - Uses OR-Tools for optimization with time dimension (service time + travel).
    - Returns: ordered bookings (list, not tuples), total hours (float), km (float), driver, ETAs (list of datetimes).
    - Enforces min 0.5 hours for short valid routes to avoid downstream skips.
    - Logs details for debugging.
    """
    if not cluster:
        return [], 0.0, 0.0, driver, []

    # Normalize input: Extract bookings and stop_types (for mixed mode)
    if cluster and isinstance(cluster[0], tuple):
        bookings = [c[0] for c in cluster]
        stop_types = [c[1] for c in cluster]
    else:
        bookings = cluster
        stop_types = stop_types or [leg_type] * len(bookings)  # Uniform if not mixed

    n = len(bookings) + 1  # +1 for hub/depot (index 0)
    manager = pywrapcp.RoutingIndexManager(n, 1, 0)  # Single vehicle, start/end at 0
    routing = pywrapcp.RoutingModel(manager)

    service_sec = 300  # Fixed service time per stop (5 min)

    # Service times: 0 at depot, service_sec at each booking
    service_times = [0] + [service_sec] * len(bookings)

    # Time callback: Travel time + service at destination
    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return time_matrix[from_node][to_node] + service_times[to_node]

    transit_callback = routing.RegisterTransitCallback(time_callback)

    # Add time dimension with slack for flexibility
    routing.AddDimension(
        transit_callback,
        900,  # slack_max
        48 * 3600,  # horizon
        True,  # fix_start_cumul_to_zero
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    # Apply time windows if provided (e.g., scheduled_pickup_at as seconds since now)
    if time_windows:
        for i, (start, end) in enumerate(time_windows, start=1):
            index = manager.NodeToIndex(i)
            time_dimension.CumulVar(index).SetRange(start, end)

    # Cost: Scaled distance (objective to minimize km)
    def dist_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(
            distance_matrix[from_node][to_node] * 1000
        )  # Meters for integer precision

    dist_callback_index = routing.RegisterTransitCallback(dist_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(dist_callback_index)

    # Search parameters: Robust for small-medium problems
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC  # Good for TSP
    )
    search_parameters.time_limit.seconds = 10  # Allow more time to find solution

    solution = routing.SolveWithParameters(search_parameters)

    if solution:
        # Extract ordered bookings and accumulate time/distance
        ordered = []
        etas = []
        route_distance_km = 0.0
        route_time_sec = 0.0
        index = routing.Start(0)
        previous_index = index
        start_time = timezone.now()  # Base for ETAs

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:  # Exclude depot
                ordered.append(bookings[node - 1])
                eta = start_time + timedelta(seconds=route_time_sec)
                etas.append(eta)

            # Accumulate for next
            from_node = manager.IndexToNode(previous_index)
            to_node = manager.IndexToNode(index)
            route_distance_km += distance_matrix[from_node][to_node]
            route_time_sec += time_matrix[from_node][to_node]

            previous_index = index
            index = solution.Value(routing.NextVar(index))

            logger.debug(f"Route time accumulation: {route_time_sec / 3600:.1f}h after {len(ordered)} stops")

        # Total hours: Round and enforce min for short routes (e.g., close stops)
        total_time_hours = round(route_time_sec / 3600.0, 3)
        if 0 < total_time_hours < 0.5:  # Avoid skips in tasks.py
            total_time_hours = 0.5

        # Debug log
        logger.debug(
            f"TSP solution found: {len(ordered)} stops, {total_time_hours:.2f}h, {route_distance_km:.1f}km"
        )

        return ordered, total_time_hours, route_distance_km, driver, etas
    else:
        # Failure: Log samples for diagnosis (e.g., zero matrices?)
        logger.error(
            f"TSP no solution - time_matrix sample: {time_matrix[:2]}, distance_matrix sample: {distance_matrix[:2]}"
        )
        return [], 0.0, 0.0, driver, []


def optimize_routes(
    bookings,
    drivers: Optional[QuerySet["DriverProfile"]] = None,  # Type hint for clarity
    hub_lat=None,
    hub_lng=None,
    time_windows=None,
    stop_types=None,
    leg_type="pickup",
):
    """
    Main optimization entrypoint: Tries VRP for multi-vehicle routing; falls back to clustering + TSP.
    - Fetches initial time/distance matrices for all bookings.
    - Bypasses VRP for small problems (<=4 bookings) to avoid solver failures.
    - Supports mixed mode via stop_types (list matching bookings).
    - Returns list of tuples: (ordered_bookings, hours, km, driver, etas)
    - If FORCE_FALLBACK=True, skips VRP entirely.
    """
    # Safeguard: Evaluate to list if QuerySet (prevents lazy issues)
    bookings = list(bookings) if isinstance(bookings, QuerySet) else bookings
    if not bookings:
        logger.info("No bookings to optimize - returning empty routes")
        return []

    # Normalize stop_types if not provided or invalid (uniform leg_type)
    if not isinstance(stop_types, list) or len(stop_types) != len(bookings):
        logger.warning(f"Invalid stop_types: resetting to uniform '{leg_type}'")
        stop_types = [leg_type] * len(bookings)

    # Extract addresses based on stop_types (for initial matrix)
    addresses = [
        b.pickup_address if typ == "pickup" else b.dropoff_address
        for b, typ in zip(bookings, stop_types)
    ]

    # Compute global matrices (used in VRP or as fallback initial)
    time_matrix, distance_matrix = get_time_matrix(
        addresses, hub_lat=hub_lat, hub_lng=hub_lng
    )

    # Force fallback if small or configured
    if FORCE_FALLBACK or len(bookings) <= 4:
        logger.info(
            f"Forcing clustering fallback (small n={len(bookings)} or FORCE_FALLBACK=True)"
        )
        return _clustering_fallback(
            bookings,
            drivers,
            hub_lat,
            hub_lng,
            time_windows,
            stop_types,
            leg_type,
            time_matrix,
            distance_matrix,
        )

    # ============ OR-TOOLS VRP (for larger n, multi-driver) ============
    try:
        n = len(bookings) + 1  # +1 for hub/depot
        num_vehicles = (
            len(drivers) if drivers else 1
        )  # Safeguard: use len only if drivers iterable
        depot = 0  # Hub

        manager = pywrapcp.RoutingIndexManager(n, num_vehicles, depot)
        routing = pywrapcp.RoutingModel(manager)

        # Time callback (similar to TSP)
        service_sec = 300
        service_times = [0] + [service_sec] * len(bookings)

        def time_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return time_matrix[from_node][to_node] + service_times[to_node]

        transit_callback = routing.RegisterTransitCallback(time_callback)

        # Time dimension (multi-vehicle compatible)
        routing.AddDimension(
            transit_callback,
            900,  # slack_max
            48 * 3600,  # horizon
            True,  # fix_start_cumul_to_zero
            "Time",
        )
        time_dimension = routing.GetDimensionOrDie("Time")

        # Time windows (per booking)
        if time_windows:
            for vehicle_id in range(num_vehicles):
                for i, (start, end) in enumerate(time_windows, start=1):
                    index = manager.NodeToIndex(i)
                    time_dimension.CumulVar(index).SetRange(start, end)
                    # Vehicle start/end at 0 (no window at depot)
                    routing.AddVariableMinimizedByFinalizer(
                        time_dimension.CumulVar(routing.Start(vehicle_id))
                    )
                    routing.AddVariableMinimizedByFinalizer(
                        time_dimension.CumulVar(routing.End(vehicle_id))
                    )

        # Distance cost (minimize total km)
        def dist_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return int(distance_matrix[from_node][to_node] * 1000)

        dist_callback_index = routing.RegisterTransitCallback(dist_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(dist_callback_index)

        # Capacity dimensions (e.g., weight/volume if drivers have limits) - add if needed
        # Example: routing.AddDimensionWithVehicleCapacity(...) for weight

        # Search parameters (robust for VRP)
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = 30  # Longer for VRP

        solution = routing.SolveWithParameters(search_parameters)

        if solution:
            routes = []
            for vehicle_id in range(num_vehicles):
                # Extract per-vehicle route (similar to TSP extraction)
                ordered = []
                etas = []
                route_distance_km = 0.0
                route_time_sec = 0.0
                index = routing.Start(vehicle_id)
                previous_index = index
                start_time = timezone.now()

                while not routing.IsEnd(index):
                    node = manager.IndexToNode(index)
                    if node != 0:
                        ordered.append(bookings[node - 1])
                        eta = start_time + timedelta(seconds=route_time_sec)
                        etas.append(eta)

                    from_node = manager.IndexToNode(previous_index)
                    to_node = manager.IndexToNode(index)
                    route_distance_km += distance_matrix[from_node][to_node]
                    route_time_sec += time_matrix[from_node][to_node]

                    previous_index = index
                    index = solution.Value(routing.NextVar(index))

                total_time_hours = round(route_time_sec / 3600.0, 3)
                if 0 < total_time_hours < 0.5:
                    total_time_hours = 0.5

                driver = drivers[vehicle_id] if drivers else None
                routes.append(
                    (ordered, total_time_hours, route_distance_km, driver, etas)
                )

            logger.info(f"VRP succeeded: {len(routes)} routes created")
            return routes
        else:
            logger.error(
                f"VRP no solution - time_matrix sample: {time_matrix[:2]}, distance_matrix sample: {distance_matrix[:2]}"
            )
            raise ValueError("VRP solver found no solution")

    except Exception as e:
        logger.error(
            f"VRP failed ({str(e)}) → using clustering fallback", exc_info=True
        )

    # Fallback if VRP fails
    return _clustering_fallback(
        bookings,
        drivers,
        hub_lat,
        hub_lng,
        time_windows,
        stop_types,
        leg_type,
        time_matrix,
        distance_matrix,
    )


def _clustering_fallback(
    bookings,
    drivers,
    hub_lat,
    hub_lng,
    time_windows,
    stop_types,
    leg_type,
    time_matrix,  # Unused in fallback (recomputed per cluster)
    distance_matrix,  # Unused in fallback
):
    """
    Clustering fallback: Groups bookings into clusters, then optimizes each as a single route (TSP).
    - Determines num_clusters based on drivers or reasonable max (e.g., 1 cluster per 4 bookings).
    - Recomputes matrices per cluster for accuracy (critical for non-zero hours).
    - Assigns drivers from pool (sorted by remaining hours).
    - Handles mixed mode via cluster_types.
    - Updates driver shifts transactionally.
    """
    logger.info("Running clustering fallback")

    # Safeguard: Ensure drivers is iterable; treat invalid types (e.g., Decimal) as empty
    drivers = list(drivers) if isinstance(drivers, QuerySet) else drivers
    if not isinstance(drivers, (list, tuple)):
        logger.error(
            f"Invalid drivers type: {type(drivers)} - treating as empty list to prevent crash"
        )
        drivers = []

    # Cap clusters to available drivers or reasonable size
    max_reasonable_clusters = max(1, len(bookings) // 4 + 1)
    num_clusters = min(len(drivers) or 1, max_reasonable_clusters)

    clusters = cluster_bookings(
        bookings,
        num_clusters=num_clusters,
        hub_lat=hub_lat,
        hub_lng=hub_lng,
        stop_types=stop_types,
    )

    # Sort drivers by remaining shift hours (descending)
    driver_pool = sorted(
        drivers,
        key=lambda d: DriverShift.get_or_create_today(d).remaining_hours,
        reverse=True,
    )

    routes = []
    # Sort clusters largest-first for better load balancing
    sorted_clusters = sorted(clusters.values(), key=len, reverse=True)

    for cluster_list in sorted_clusters:
        if not cluster_list:
            logger.debug("Skipping empty cluster")
            continue

        # Normalize cluster (handles mixed tuples)
        if cluster_list and isinstance(cluster_list[0], tuple):
            target_bookings = [item[0] for item in cluster_list]
            cluster_types = [item[1] for item in cluster_list]
            logger.debug(f"Mixed cluster: {len(target_bookings)} bookings")
        else:
            target_bookings = cluster_list
            cluster_types = [leg_type] * len(target_bookings)
            logger.debug(f"Uniform cluster: {len(target_bookings)} bookings")

        if not target_bookings:
            logger.warning("Cluster normalized to empty list — skipping")
            continue

        # Assign next available driver
        driver = driver_pool.pop(0) if driver_pool else None

        # Recompute matrices specifically for this cluster (ensures accuracy)
        cluster_addresses = [
            b.pickup_address if typ == "pickup" else b.dropoff_address
            for b, typ in zip(target_bookings, cluster_types)
        ]
        time_matrix_cluster, distance_matrix_cluster = get_time_matrix(
            cluster_addresses, hub_lat=hub_lat, hub_lng=hub_lng
        )
        logger.debug(
            f"Recomputed cluster matrix for {len(cluster_addresses)} addresses"
        )

        # Optimize single cluster route
        ordered, hrs, km, _, etas = optimize_route_single(
            target_bookings,
            time_matrix_cluster,
            distance_matrix_cluster,
            driver=driver,
            time_windows=time_windows,
            stop_types=cluster_types,
            leg_type=leg_type,
        )

        # Accumulate load (weight/volume)
        total_weight = sum(float(getattr(b.quote, "weight_kg", 0)) for b in ordered)
        total_volume = sum(float(getattr(b.quote, "volume_m3", 0)) for b in ordered)

        routes.append((ordered, hrs, km, driver, etas))

        # Update driver's shift if assigned
        if driver:
            shift = DriverShift.get_or_create_today(driver)
            with transaction.atomic():
                current = shift.current_load or {
                    "hours": 0.0,
                    "weight": 0.0,
                    "volume": 0.0,
                }
                shift.current_load = {
                    "hours": current["hours"] + hrs,
                    "weight": current["weight"] + total_weight,
                    "volume": current["volume"] + total_volume,
                }
                shift.save()

    logger.info(
        f"Fallback created {len(routes)} routes ({sum(1 for r in routes if r[3])} assigned)"
    )
    return routes

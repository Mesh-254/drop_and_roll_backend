# bookings/utils/route_optimization.py
from sklearn.cluster import KMeans
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from django.utils import timezone
from datetime import timedelta
import logging
from driver.models import DriverShift
from django.db import transaction

logger = logging.getLogger(__name__)

def cluster_bookings(bookings, num_clusters=5, hub_lat=None, hub_lng=None):
    if not bookings:
        return {}

    coords = []
    valid_bookings = []
    invalid_bookings = []

    for b in bookings:
        addr = b.pickup_address if hasattr(b, 'pickup_address') and b.pickup_address else b.dropoff_address
        if addr and addr.latitude is not None and addr.longitude is not None:
            coords.append([float(addr.latitude), float(addr.longitude)])
            valid_bookings.append(b)
        else:
            invalid_bookings.append(b)

    if len(valid_bookings) == 0:
        return {0: bookings}

    original_coords_len = len(coords)

    if hub_lat is not None and hub_lng is not None and original_coords_len > 1:
        hub_coord = [float(hub_lat), float(hub_lng)]
        # Add hub multiple times to pull clusters toward it
        multiples = max(5, original_coords_len // 3)
        coords = [hub_coord] * multiples + coords

    k = min(num_clusters, len(valid_bookings)) or 1
    kmeans = KMeans(n_clusters=k, random_state=0, n_init=10)
    labels = kmeans.fit_predict(coords)

    # Slice only booking labels if hub was added
    booking_labels = labels[multiples if 'multiples' in locals() else 0 : multiples + original_coords_len if 'multiples' in locals() else None]

    clusters = {i: [] for i in range(k)}
    for label, booking in zip(booking_labels, valid_bookings):
        clusters[label].append(booking)

    # Distribute invalid bookings round-robin
    for i, b in enumerate(invalid_bookings):
        clusters[i % k].append(b)

    return clusters


def optimize_route_single(cluster, time_matrix, distance_matrix, driver=None, time_windows=None):
    if not cluster:
        return [], 0.0, 0.0, driver, []

    n = len(cluster) + 1
    manager = pywrapcp.RoutingIndexManager(n, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    service_sec = 300  # 5 minutes

    service_times = [0] + [service_sec] * len(cluster)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return time_matrix[from_node][to_node] + service_times[from_node]

    transit_callback = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback)

    # Time dimension with 5 min slack
    routing.AddDimension(
        transit_callback,
        300,   # max slack per vehicle
        24 * 3600,
        True,
        'Time'
    )
    time_dimension = routing.GetDimensionOrDie('Time')

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.time_limit.seconds = 60

    solution = routing.SolveWithParameters(search_parameters)

    now = timezone.now()

    if solution:
        ordered = []
        etas = []
        route_distance_km = 0.0

        index = routing.Start(0)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                ordered.append(cluster[node - 1])
                eta_sec = solution.Value(time_dimension.CumulVar(index))
                etas.append(now + timedelta(seconds=eta_sec))

            next_index = solution.Value(routing.NextVar(index))
            from_node = node
            to_node = manager.IndexToNode(next_index)
            route_distance_km += distance_matrix[from_node][to_node]

            index = next_index

        total_time_sec = solution.Value(time_dimension.CumulVar(index))
        total_time_hours = round(total_time_sec / 3600.0, 3)
        total_distance_km = round(route_distance_km, 3)

        return ordered, total_time_hours, total_distance_km, driver, etas

    else:
        logger.warning("TSP failed → using arbitrary order")
        ordered = cluster[:]
        etas = [now + timedelta(minutes=40 * i) for i in range(len(cluster))]  # reasonable spacing
        total_hours = round((len(cluster) * 0.5) + 2.0, 3)  # ~30 min avg per stop incl travel
        return ordered, total_hours, 80.0, driver, etas


def optimize_routes(bookings, hub_lat, hub_lng, time_matrix, distance_matrix, drivers, time_windows=None, leg_type='pickup'):
    if not bookings:
        logger.info(f"No bookings for {leg_type}")
        return []

    logger.info(f"Optimizing {len(bookings)} {leg_type}(s) with {len(drivers)} driver(s)")

    routes = []
    service_sec = 300

    try:
        # ============ FULL VRP  SOLVER (always attempted) ============
        n = len(bookings) + 1
        orig_m = len(drivers)
        dummy = False
        if orig_m == 0:
            m = 1  # Use dummy vehicle to allow VRP to run
            dummy = True
        else:
            m = orig_m

        manager = pywrapcp.RoutingIndexManager(n, m, 0)
        routing = pywrapcp.RoutingModel(manager)

        service_times = [0] + [service_sec] * len(bookings)

        def time_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            
            travel_time = time_matrix[from_node][to_node]
            service_time = service_times[from_node]

            return int(round(travel_time + service_time))

        transit_callback_index = routing.RegisterTransitCallback(time_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        # Time dimension
        routing.AddDimension(
            transit_callback_index,
            300,  # 5 min slack
            24 * 3600, # max time per vehicle
            True,
            'Time'
        )
        time_dimension = routing.GetDimensionOrDie('Time')

        # Vehicle time limits - allow up to remaining + 4h overtime
        for v in range(m):
            if dummy:
                max_hours = 24.0  # Large default for dummy
            else:
                shift = DriverShift.get_or_create_today(drivers[v])
                max_hours = shift.remaining_hours + 4.0
            max_sec = round(max_hours * 3600)
            time_dimension.SetSpanUpperBoundForVehicle(max_sec, v)
            routing.SetFixedCostOfVehicle(50000, v)  # encourage balanced use

        # Weight dimension
        def weight_callback(from_index):
            node = manager.IndexToNode(from_index)
            return 0 if node == 0 else int(float(bookings[node - 1].quote.weight_kg or 0) * 1000)

        weight_index = routing.RegisterUnaryTransitCallback(weight_callback)
        weight_capacities = [int(float(d.max_weight_kg or 1000) * 1000) for d in drivers] if not dummy else [10000000] * m
        routing.AddDimensionWithVehicleCapacity(
            weight_index,
            0,
            weight_capacities,
            True,
            'Weight'
        )

        # Volume dimension
        def volume_callback(from_index):
            node = manager.IndexToNode(from_index)
            return 0 if node == 0 else int(float(bookings[node - 1].quote.volume_m3 or 0) * 1000)

        volume_index = routing.RegisterUnaryTransitCallback(volume_callback)
        volume_capacities = [int((d.max_volume_m3 or 10) * 1000) for d in drivers] if not dummy else [100000] * m
        routing.AddDimensionWithVehicleCapacity(
            volume_index,
            0,
            volume_capacities,
            True,
            'Volume'
        )

        # Soft time windows
        if time_windows:
            now = timezone.now()
            for i in range(1, n):
                start, end = time_windows[i - 1]
                if not start or not end:
                    continue
                start_sec = max(0, int((start - now).total_seconds()))
                end_sec = int((end - now).total_seconds())
                index = manager.NodeToIndex(i)
                time_dimension.SetCumulVarSoftLowerBound(index, start_sec, 1000)
                time_dimension.SetCumulVarSoftUpperBound(index, end_sec, 10000)

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_parameters.time_limit.seconds = 300
        search_parameters.solution_limit = 20000

        solution = routing.SolveWithParameters(search_parameters)

        if solution:
            now = timezone.now()
            for v in range(m):
                index = routing.Start(v)
                if not routing.IsVehicleUsed(solution, v):
                    continue

                ordered = []
                etas = []
                route_distance_km = 0.0

                while not routing.IsEnd(index):
                    node = manager.IndexToNode(index)
                    if node != 0:
                        ordered.append(bookings[node - 1])
                        eta_sec = solution.Value(time_dimension.CumulVar(index))
                        etas.append(now + timedelta(seconds=eta_sec))

                    next_index = solution.Value(routing.NextVar(index))
                    from_node = node
                    to_node = manager.IndexToNode(next_index)
                    route_distance_km += distance_matrix[from_node][to_node]

                    index = next_index

                total_time_sec = solution.Value(time_dimension.CumulVar(index))
                total_time_hours = round(total_time_sec / 3600.0, 3)
                total_distance_km = round(route_distance_km, 3)

                driver = drivers[v] if v < orig_m else None
                if driver:
                    shift = DriverShift.get_or_create_today(driver)

                    total_weight = sum(float(b.quote.weight_kg or 0) for b in ordered)
                    total_volume = sum(float(b.quote.volume_m3 or 0) for b in ordered)

                    with transaction.atomic():
                        current = shift.current_load or {'hours': 0.0, 'weight': 0.0, 'volume': 0.0}
                        shift.current_load = {
                            'hours': current['hours'] + total_time_hours,
                            'weight': current['weight'] + total_weight,
                            'volume': current['volume'] + total_volume,
                        }
                        shift.save()

                routes.append((ordered, total_time_hours, total_distance_km, driver, etas))

            logger.info(f"VRP succeeded: {len(routes)} routes created{' (with dummy vehicle)' if dummy else ''}")
            return routes
        else:
            raise ValueError("VRP solver found no solution")

    except Exception as e:
        logger.error(f"VRP failed ({e}) → using clustering fallback")

    # ============ CLUSTERING FALLBACK (guaranteed assignment) ============
    logger.info("Running clustering fallback")

    max_reasonable_clusters = max(1, len(bookings) // 4 + 1)
    num_clusters = min(len(drivers), max_reasonable_clusters) if drivers else 1

    clusters = cluster_bookings(
        bookings,
        num_clusters=num_clusters,
        hub_lat=hub_lat,
        hub_lng=hub_lng
    )

    # Sort drivers by remaining hours (most available first)
    driver_pool = sorted(
        drivers,
        key=lambda d: DriverShift.get_or_create_today(d).remaining_hours if hasattr(d, 'DriverShift') else 0,
        reverse=True
    ) if drivers else []

    sorted_clusters = sorted(clusters.values(), key=len, reverse=True)

    for cluster_list in sorted_clusters:
        if not cluster_list:
            continue

        driver = driver_pool.pop(0) if driver_pool else None

        ordered, hrs, km, _, etas = optimize_route_single(
            cluster_list, time_matrix, distance_matrix, driver, time_windows
        )

        total_weight = sum(float(b.quote.weight_kg or 0) for b in ordered)
        total_volume = sum(float(b.quote.volume_m3 or 0) for b in ordered)

        routes.append((ordered, hrs, km, driver, etas))

        if driver:
            shift = DriverShift.get_or_create_today(driver)
            with transaction.atomic():
                current = shift.current_load or {'hours': 0.0, 'weight': 0.0, 'volume': 0.0}
                shift.current_load = {
                    'hours': current['hours'] + hrs,
                    'weight': current['weight'] + total_weight,
                    'volume': current['volume'] + total_volume,
                }
                shift.save()

    logger.info(f"Fallback created {len(routes)} routes ({len([r for r in routes if r[3]])} assigned)")
    return routes
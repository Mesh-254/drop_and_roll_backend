# bookings/utils/route_optimization.py
from sklearn.cluster import KMeans  #used for fallback needed
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

def cluster_bookings(bookings, num_clusters=5):  # NEW: Re-added as fallback if VRP fails
    if not bookings:
        return {}
    coords = np.array([[b.pickup_address.latitude, b.pickup_address.longitude]
                       for b in bookings if b.pickup_address.latitude is not None])
    if len(coords) == 0:
        return {0: bookings}
    kmeans = KMeans(n_clusters=min(num_clusters, len(coords)), random_state=0)
    labels = kmeans.fit_predict(coords)
    clusters = {i: [] for i in range(max(labels)+1)}
    for idx, label in enumerate(labels):
        clusters[label].append(bookings[idx])
    return clusters
def optimize_route_single(cluster, time_matrix, distance_matrix, driver, time_windows=None):  # NEW: Fallback single-vehicle TSP
    # Similar to old optimize_route, but for one driver
    n = len(cluster) + 1
    manager = pywrapcp.RoutingIndexManager(n, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return time_matrix[from_node][to_node]

    transit_callback = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.time_limit.seconds = 30

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return [], 0.0, 0.0, driver

    ordered = []
    etas = []
    prev_node = 0
    route_time_sec = 0
    route_distance_km = 0.0
    while not routing.IsEnd(solution.Value(routing.Start(0))):
        node = manager.IndexToNode(solution.Value(routing.NextVar(prev_node)))
        if node != 0:
            ordered.append(cluster[node - 1])
            etas.append(timezone.now() + timedelta(seconds=route_time_sec))  # Approx eta
        travel_sec = time_matrix[prev_node][node]
        travel_km = distance_matrix[prev_node][node]
        route_time_sec += travel_sec
        route_distance_km += travel_km
        prev_node = node

    # Back to hub
    travel_sec = time_matrix[prev_node][0]
    travel_km = distance_matrix[prev_node][0]
    route_time_sec += travel_sec
    route_distance_km += travel_km

    route_time_sec += len(ordered) * 300  # Service time

    total_time_hours = round(route_time_sec / 3600.0, 3)
    total_distance_km = round(route_distance_km, 3)

    return ordered, total_time_hours, total_distance_km, driver, etas

def optimize_routes(bookings, hub_lat, hub_lng, time_matrix, distance_matrix, drivers, time_windows=None, leg_type='pickup'):
    """Optimize multiple routes using multi-vehicle VRP with capacities, time.
    bookings: list of Booking objects
    hub_lat, hub_lng: hub coords (for logging)
    time_matrix: [[sec]] n x n
    distance_matrix: [[km]] n x n
    drivers: list of DriverProfile with active shift
    time_windows: list of (start_sec, end_sec) or None
    Returns: list of (ordered_bookings, total_time_hours, total_distance_km, driver, etas)
    """
    if not bookings or not drivers:
        logger.warning(f"No bookings ({len(bookings)}) or drivers ({len(drivers)}) for {leg_type}")  # NEW: Log counts
        return []

    # NEW: Log fetched bookings for debugging
    logger.info(f"Optimizing {len(bookings)} bookings for {leg_type}: IDs {[b.id for b in bookings]}")

    n = len(bookings) + 1  # 0=hub, 1..n=bookings
    m = len(drivers)  # vehicles = drivers

    # NEW: Log input sizes
    logger.debug(f"VRP setup: {n} nodes, {m} vehicles, time_matrix shape {np.array(time_matrix).shape}")

    manager = pywrapcp.RoutingIndexManager(n, m, 0)

    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        if from_node >= len(time_matrix) or to_node >= len(time_matrix[0]):
            return 3600 * 10  # Huge penalty for invalid
        return time_matrix[from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(time_callback)

    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    service_sec = 300  # 5 min per stop
    slack_max = 3600  # 1 hour wait max per stop
    global_time_max = int(8 * 3600)  # 8 hours global max (but per-vehicle overridden)

    routing.AddDimension(
        transit_callback_index,
        slack_max,
        global_time_max,
        True,
        'Time'
    )
    time_dimension = routing.GetDimensionOrDie('Time')

    for node in range(1, n):
        index = manager.NodeToIndex(node)
        routing.AddConstantDimension(service_sec, global_time_max, True, 'Service')

    for v in range(m):
        max_sec = int(drivers[v].remaining_hours * 3600)
        time_dimension.SetSpanUpperBoundForVehicle(max_sec, v)
        routing.SetFixedCostOfVehicle(100000, v)  # High to pack densely

    def demand_weight_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        if from_node == 0:
            return 0
        return int(bookings[from_node - 1].quote.weight_kg)

    demand_weight_index = routing.RegisterUnaryTransitCallback(demand_weight_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_weight_index,
        0,
        [int(d.max_weight_kg) for d in drivers],
        True,
        'Weight'
    )

    def demand_volume_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        if from_node == 0:
            return 0
        return int(bookings[from_node - 1].quote.volume_m3 * 1000)  # Scale

    demand_volume_index = routing.RegisterUnaryTransitCallback(demand_volume_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_volume_index,
        0,
        [int(d.max_volume_m3 * 1000) for d in drivers],
        True,
        'Volume'
    )

    # Time windows (relaxed)
    if time_windows:
        now_sec = int(timezone.now().timestamp())
        for i in range(1, n):
            sched = time_windows[i-1]
            if sched:
                ts = int(sched.timestamp())
                start = max(0, ts - 14400)
                end = ts + 14400
            else:
                start = now_sec
                end = now_sec + 86400 * 3
            if start >= end:
                start = 0
                end = 86400 * 7
            index = manager.NodeToIndex(i)
            time_dimension.CumulVar(index).SetRange(start, end)

    drop_penalty = 1000000
    for node in range(1, n):
        routing.AddDisjunction([manager.NodeToIndex(node)], drop_penalty)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 120  # MODIFIED: Increase to 120s for harder instances
    search_parameters.solution_limit = 10000
    search_parameters.log_search = True  # NEW: Log solver progress for debug

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        logger.error(f"VRP failed for {len(bookings)} bookings, {m} drivers - falling back to KMeans + single-route")  # NEW: Log failure
        # NEW: Fallback to KMeans clustering + single-route per cluster
        clusters = cluster_bookings(bookings, min(5, len(drivers)))
        routes = []
        for cluster_id, cluster in clusters.items():
            if cluster and drivers:  # Assign one driver per cluster
                driver = drivers.pop(0)  # Simple round-robin
                ordered, total_time_hours, total_distance_km, _, etas = optimize_route_single(cluster, time_matrix, distance_matrix, driver, time_windows)
                if ordered:
                    routes.append((ordered, total_time_hours, total_distance_km, driver, etas))
        return routes

    # NEW: Extract routes per vehicle
    routes = []
    now = timezone.now()
    for v in range(m):
        index = routing.Start(v)
        if routing.IsVehicleDropped(v):  # Skip unused
            continue
        ordered = []
        etas = []  # For ordered_stops eta
        prev_node = manager.IndexToNode(index)
        route_time_sec = 0
        route_distance_km = 0.0
        cumul_time_sec = 0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                ordered.append(bookings[node - 1])
                # NEW: Cumul time for eta
                cumul_var = time_dimension.CumulVar(index)
                cumul_time_sec = solution.Value(cumul_var)
                eta = now + timedelta(seconds=cumul_time_sec)
                etas.append(eta)
            travel_sec = time_matrix[prev_node][node]
            travel_km = distance_matrix[prev_node][node]
            route_time_sec += travel_sec
            route_distance_km += travel_km
            prev_node = node
            index = solution.Value(routing.NextVar(index))
        # Back to hub
        travel_sec = time_matrix[prev_node][0]
        travel_km = distance_matrix[prev_node][0]
        route_time_sec += travel_sec
        route_distance_km += travel_km

        # Add service times
        route_time_sec += len(ordered) * service_sec

        total_time_hours = round(route_time_sec / 3600.0, 3)  # MODIFIED: Round to 3 decimals
        total_distance_km = round(route_distance_km, 3)  # MODIFIED: Round to 3 decimals

        # NEW: Min time check (revenue risk mitigation)
        min_threshold = 4.0  # Example: Flag if <4 hours
        if total_time_hours < min_threshold:
            logger.warning(f"Short route {total_time_hours:.3f}h for driver {drivers[v].id} - consider requeuing")
            # Optional: Skip creation, requeue bookings (not implemented here)

        if ordered:  # Only if route used
            logger.info(f"Route for driver {v}: {len(ordered)} stops, {total_time_hours:.3f}h, {total_distance_km:.3f}km")
            routes.append((ordered, total_time_hours, total_distance_km, drivers[v], etas))  # Added etas

    logger.info(f"VRP success: {len(routes)} routes created")
    return routes
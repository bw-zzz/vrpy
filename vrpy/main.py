from networkx import DiGraph, shortest_path
import logging
from pandas import DataFrame
from time import time

from vrpy.master_solve_pulp import MasterSolvePulp
from vrpy.subproblem_lp import SubProblemLP
from vrpy.subproblem_cspy import SubProblemCSPY

logger = logging.getLogger(__name__)


class VehicleRoutingProblem:
    """Stores the underlying network of the VRP and parameters for solving
       with a column generation approach.

    Args:
        G (DiGraph): The underlying network.
        initial_routes (list, optional):
            List of paths (list of nodes).
            Feasible solution for first iteration.
            Defaults to None.
        edge_cost_function (function, optional):
            Mapping with a cost for each edge.
            Only necessary if initial_routes is not None.
            Defaults to None.
        num_stops (int, optional):
            Maximum number of stops.
            Defaults to None.
        load_capacity (int, optional):
            Maximum load per vehicle.
            Defaults to None.
        duration (int, optional):
            Maximum duration of route.
            Defaults to None.
        time_windows (bool, optional):
            True if time windows on vertices.
            Defaults to False.
        pickup_delivery (bool, optional):
            True if pickup and delivery constraints.
            Defaults to False.
        distribution_collection (bool, optional):
            True if distribution and collection are simultaneously enforced.
            Defaults to False.
        drop_penalty (int, optional):
            Value of penalty if node is dropped.
            Defaults to None.
        undirected (bool, optional):
            True if underlying network is undirected.
            Defaults to True.
    """

    def __init__(
        self,
        G,
        initial_routes=None,
        edge_cost_function=None,
        num_stops=None,
        load_capacity=None,
        duration=None,
        time_windows=False,
        pickup_delivery=False,
        distribution_collection=False,
        drop_penalty=None,
    ):
        self.G = G
        # VRP options/constraints
        self._initial_routes = initial_routes
        self._edge_cost_function = edge_cost_function
        self._num_stops = num_stops
        self._load_capacity = load_capacity
        self._duration = duration
        self._time_windows = time_windows
        self._pickup_delivery = pickup_delivery
        self._distribution_collection = distribution_collection
        self._drop_penalty = drop_penalty

        # Set default attributes
        self.add_default_service_time()
        # Remove infeasible arcs
        self.prune_graph()

        # Compute upper bound on number of stops as knapsack problem
        if self.load_capacity:
            self.get_num_stops_upper_bound()

        # Attributes to keep track of solution
        self.best_solution = None
        self.best_routes = None
        self.iteration = []
        self.lower_bound = []

        # Keep track of paths containing nodes
        self.routes_with_node = {}

    # @profile
    def solve(self, cspy=True, exact=True, time_limit=None):
        """Iteratively generates columns with negative reduced cost and solves as MIP.

        Args:
            cspy (bool, optional):
                True if cspy is used for subproblem.
                Defaults to True.
            exact (bool, optional):
                True if only cspy's exact algorithm is used to generate columns.
                Otherwise, heuristitics will be used until they produce +ve
                reduced cost columns, after which the exact algorithm is used.
                Defaults to True.
            time_limit (int, optional):
                Maximum number of seconds allowed for solving (for finding columns).
                Defaults to None.
        Returns:
            float: Optimal solution of MIP based on generated columns
        """
        # Setup attributes if cspy
        if cspy:
            self.update_attributes_for_cspy()
            self.check_options_consistency()

        # Initialization
        more_routes = True
        if not self.initial_routes:
            self.initial_solution()
        else:
            self.initial_routes_to_digraphs()
        k = 0
        no_improvement = 0
        start = time()

        # Generate interesting columns
        while more_routes and k < 500 and no_improvement < 200:

            # Solve restricted relaxed master problem
            masterproblem = MasterSolvePulp(
                self.G,
                self.routes_with_node,
                self.routes,
                self.drop_penalty,
                relax=True,
            )
            duals, relaxed_cost = masterproblem.solve()
            logger.info("iteration %s, %s" % (k, relaxed_cost))

            # Solve sub problem heuristically
            for beta in [3, 5, 7, 9]:
                alpha = None
                logger.debug("subproblem with beta = %s" % (beta))
                subproblem = self.def_subproblem(duals, alpha, beta, cspy, exact)
                self.routes, more_routes = subproblem.solve(time_limit)
                if more_routes:
                    break

            # If no column was found, second heuristic is tried
            if not more_routes:
                for alpha in [0.3, 0.5, 0.7, 0.9]:
                    beta = None
                    logger.debug("subproblem with alpha = %s" % (alpha))
                    subproblem = self.def_subproblem(duals, alpha, beta, cspy, exact)
                    self.routes, more_routes = subproblem.solve(time_limit)
                    if more_routes:
                        break

            # If no column was found heuristically, solve subproblem exactly
            if not more_routes:
                alpha, beta = None, None
                logger.info("solving exact subproblem")
                subproblem = self.def_subproblem(duals, alpha, beta, cspy, exact)
                self.routes, more_routes = subproblem.solve(time_limit)

            # Keep track of convergence rate
            k += 1
            if k > 1 and relaxed_cost == self.lower_bound[-1]:
                no_improvement += 1
            else:
                no_improvement = 0
            self.iteration.append(k)
            self.lower_bound.append(relaxed_cost)

            # Stop if time limit is passed
            if time_limit:
                if time() - start > time_limit:
                    break

        # Solve as MIP
        logger.info("MIP solution")
        masterproblem_mip = MasterSolvePulp(
            self.G, self.routes_with_node, self.routes, self.drop_penalty, relax=False
        )
        self.best_value, self.best_routes_as_graphs = masterproblem_mip.solve()
        self.best_routes_as_node_lists()

        # Export relaxed_cost = f(iteration) to Excel file
        # self.export_convergence_rate()

    def def_subproblem(self, duals, alpha, beta, cspy, exact):
        """Instanciates the subproblem."""
        if cspy:
            # With cspy
            subproblem = SubProblemCSPY(
                self.G,
                duals,
                self.routes_with_node,
                self.routes,
                self.num_stops,
                self.load_capacity,
                self.duration,
                self.time_windows,
                self.pickup_delivery,
                self.distribution_collection,
                alpha,
                beta,
                exact=exact,
            )
        else:
            # As LP
            subproblem = SubProblemLP(
                self.G,
                duals,
                self.routes_with_node,
                self.routes,
                self.num_stops,
                self.load_capacity,
                self.duration,
                self.time_windows,
                self.pickup_delivery,
                self.distribution_collection,
                alpha,
                beta,
            )
        return subproblem

    def prune_graph(self):
        """Preprocessing:
           - Removes useless edges from graph
           - Strengthens time windows
        """
        infeasible_arcs = []
        # Remove infeasible arcs (capacities)
        if self.load_capacity:
            for (i, j) in self.G.edges():
                if (
                    self.G.nodes[i]["demand"] + self.G.nodes[j]["demand"]
                    > self.load_capacity
                ):
                    infeasible_arcs.append((i, j))

        # Remove infeasible arcs (time windows)
        if self.time_windows:
            for (i, j) in self.G.edges():
                travel_time = self.G.edges[i, j]["time"]
                service_time = self.G.nodes[i]["service_time"]
                tail_inf_time_window = self.G.nodes[i]["lower"]
                head_sup_time_window = self.G.nodes[j]["upper"]
                if (
                    tail_inf_time_window + travel_time + service_time
                    > head_sup_time_window
                ):
                    infeasible_arcs.append((i, j))

            # Strengthen time windows
            for v in self.G.nodes():
                if v not in ["Source", "Sink"]:
                    # earliest time is coming straight from depot
                    self.G.nodes[v]["lower"] = max(
                        self.G.nodes[v]["lower"],
                        self.G.nodes["Source"]["lower"]
                        + self.G.edges["Source", v]["time"],
                    )
                    # Latest time is going straight to depot
                    self.G.nodes[v]["upper"] = min(
                        self.G.nodes[v]["upper"],
                        self.G.nodes["Sink"]["upper"] - self.G.edges[v, "Sink"]["time"],
                    )
        # Remove Source-Sink
        if ("Source", "Sink") in self.G.edges():
            infeasible_arcs.append(("Source", "Sink"))

        self.G.remove_edges_from(infeasible_arcs)

    def initial_solution(self):
        """If no initial solution is given, creates one"""
        initial_routes = []
        route_id = 0
        for v in self.G.nodes():
            if v not in ["Source", "Sink"]:
                route_id += 1
                if ("Source", v) in self.G.edges():
                    cost_1 = self.G.edges["Source", v]["cost"]
                else:
                    # If edge does not exist, create it with a high cost
                    cost_1 = 1e10
                    self.G.add_edge("Source", v, cost=cost_1)
                if (v, "Sink") in self.G.edges():
                    cost_2 = self.G.edges[v, "Sink"]["cost"]
                else:
                    # If edge does not exist, create it with a high cost
                    cost_2 = 1e10
                    self.G.add_edge(v, "Sink", cost=cost_2)
                total_cost = cost_1 + cost_2
                route = DiGraph(name=route_id, cost=total_cost)
                route.add_edge("Source", v, cost=cost_1)
                route.add_edge(v, "Sink", cost=cost_2)
                initial_routes.append(route)
                self.routes_with_node[v] = [route]

        self.routes = initial_routes

    def initial_routes_to_digraphs(self):
        """Converts list of initial routes to list of Digraphs."""
        route_id = 0
        self.routes = []
        for r in self.initial_routes:
            total_cost = 0
            route_id += 1
            G = DiGraph(name=route_id)
            edges = list(zip(r[:-1], r[1:]))
            for (i, j) in edges:
                dist = round(self.edge_cost_function(i, j), 1)
                G.add_edge(i, j, cost=dist)
                total_cost += dist
            G.graph["cost"] = total_cost
            self.routes.append(G)
            for v in r:
                self.routes_with_node[v] = [G]
        for v in self.G.nodes():
            if v not in self.routes_with_node:
                self.routes_with_node[v] = []

    def update_attributes_for_cspy(self):
        """Adds dummy attributes on nodes and edges if missing."""

        if not self.load_capacity:
            for v in self.G.nodes():
                if "demand" not in self.G.nodes[v]:
                    self.G.nodes[v]["demand"] = 0
        if not self.time_windows:
            for v in self.G.nodes():
                if "lower" not in self.G.nodes[v]:
                    self.G.nodes[v]["lower"] = 0
                if "upper" not in self.G.nodes[v]:
                    self.G.nodes[v]["upper"] = 0
            for (i, j) in self.G.edges():
                if "time" not in self.G.edges[i, j]:
                    self.G.edges[i, j]["time"] = 0
        if not self.distribution_collection:
            for v in self.G.nodes():
                if "collect" not in self.G.nodes[v]:
                    self.G.nodes[v]["collect"] = 0

    def check_options_consistency(self):
        """
        The following options need are not implemented yet with cspy:
            -pickup and delivery
        """
        if self.pickup_delivery:
            raise NotImplementedError

    def add_default_service_time(self):
        """Adds dummy service time."""
        if self.duration or self.time_windows:
            for v in self.G.nodes():
                if "service_time" not in self.G.nodes[v]:
                    self.G.nodes[v]["service_time"] = 0

    def get_num_stops_upper_bound(self):
        """
        Finds upper bound on number of stops, from here :
        https://pubsonline.informs.org/doi/10.1287/trsc.1050.0118

        A knapsack problem is solved to maximize the number of
        visits, subject to capacity constraints.
        """

        def knapsack(weights, capacity):
            """
            Binary knapsack solver with identical profits of weight 1.
            Args:
                weights (list) : list of integers
                capacity (int) : maximum capacity
            Returns:
                (int) : maximum number of objects
            """
            n = len(weights)
            # sol : [items, remaining capacity]
            sol = [[0] * (capacity + 1) for i in range(n)]
            added = [[False] * (capacity + 1) for i in range(n)]
            for i in range(n):
                for j in range(capacity + 1):
                    if weights[i] > j:
                        sol[i][j] = sol[i - 1][j]
                    else:
                        sol_add = 1 + sol[i - 1][j - weights[i]]
                        if sol_add > sol[i - 1][j]:
                            sol[i][j] = sol_add
                            added[i][j] = True
                        else:
                            sol[i][j] = sol[i - 1][j]
            return sol[n - 1][capacity]

        # Maximize sum of vertices such that sum of demands respect capacity constraints
        demands = [int(self.G.nodes[v]["demand"]) for v in self.G.nodes()]
        # Solve the knapsack problem
        max_num_stops = knapsack(demands, self.load_capacity)
        # Update num_stops attribute
        if self._num_stops:
            self._num_stops = min(max_num_stops, self.num_stops)
        else:
            self._num_stops = max_num_stops
        logger.info("new upper bound : max num stops = %s" % self.num_stops)

    def best_routes_as_node_lists(self):
        """Converts route as DiGraph to route as node list."""
        self.best_routes = []
        for route in self.best_routes_as_graphs:
            node_list = shortest_path(route, "Source", "Sink")
            self.best_routes.append(node_list)

    def export_convergence_rate(self):
        """Exports evolution of lowerbound to excel file.
        """
        keys = ["k", "z"]
        values = [self.iteration, self.lower_bound]
        convergence = dict(zip(keys, values))
        df = DataFrame(convergence, columns=keys)
        df.to_excel("convergence.xls", index=False)

    @property
    def initial_routes(self):
        return self._initial_routes

    @property
    def edge_cost_function(self):
        return self._edge_cost_function

    @property
    def num_stops(self):
        return self._num_stops

    @property
    def load_capacity(self):
        return self._load_capacity

    @property
    def duration(self):
        return self._duration

    @property
    def time_windows(self):
        return self._time_windows

    @property
    def pickup_delivery(self):
        return self._pickup_delivery

    @property
    def distribution_collection(self):
        return self._distribution_collection

    @property
    def drop_penalty(self):
        return self._drop_penalty

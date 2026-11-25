"""Pure octilinear router (B-C12): obstacle grid, A* with 0/45-degree turns only, exact pad
approach, coupled pairs and the `run_cpu` entry points. No kipy import."""

from __future__ import annotations

from copperchat.board.router.grid import (
    MAX_CELLS_PER_LAYER,
    WINDOW_MM,
    ObstacleGrid,
    RoutePath,
    RouteRequest,
    RouteTooLarge,
    build_grid,
    prune_model,
)
from copperchat.board.router.astar import route, solve
from copperchat.board.router.pairs import pair_coupling, route_pair, solve_pair

__all__ = [
    "MAX_CELLS_PER_LAYER",
    "WINDOW_MM",
    "ObstacleGrid",
    "RoutePath",
    "RouteRequest",
    "RouteTooLarge",
    "build_grid",
    "pair_coupling",
    "prune_model",
    "route",
    "route_pair",
    "solve",
    "solve_pair",
]

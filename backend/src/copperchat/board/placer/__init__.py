"""Placer (B-C11, SP4 P4): clusters, cluster layout, block packing, legaliser, score.
Pure: no kipy import, deterministic."""

from copperchat.board.placer.cluster import Cluster, build_clusters
from copperchat.board.placer.layout import Move, apply_moves, layout_cluster, moved_part
from copperchat.board.placer.legalize import DoesNotFit, edge_margin, keepout_polys, legalize
from copperchat.board.placer.pack import (
    PlaceResult,
    SidePolicyError,
    block_extent,
    block_layout,
    place_block,
)
from copperchat.board.placer.score import MAX_CELL_PINS, PlacementScore, score_placement

__all__ = [
    "MAX_CELL_PINS",
    "Cluster",
    "DoesNotFit",
    "Move",
    "PlaceResult",
    "PlacementScore",
    "SidePolicyError",
    "apply_moves",
    "block_extent",
    "block_layout",
    "build_clusters",
    "edge_margin",
    "keepout_polys",
    "layout_cluster",
    "legalize",
    "moved_part",
    "place_block",
    "score_placement",
]

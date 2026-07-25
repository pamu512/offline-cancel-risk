from statistics import mean
from typing import Any

import numpy as np
from sklearn.cluster import DBSCAN

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.timeutil import parse_ts


def compute_stop_confidences(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    is_round_trip = bool(stops) and stops[0] == stops[-1]

    if not points:
        return {
            "confidence_list": [],
            "unique_clusters": 0,
            "is_round_trip": is_round_trip,
            "pk_visited_times": 0,
            "final_confidence": 0.0,
        }

    coords = np.array([[p.lat, p.lon] for p in points], dtype=float)
    labels = DBSCAN(
        eps=policy["clustering_radius_m"] / 6371000,
        min_samples=policy["min_pts"],
        metric="haversine",
    ).fit(np.radians(coords)).labels_
    unique_clusters = len(set(labels)) - (1 if -1 in labels else 0)

    immediate_r = policy["immediate_dp_radius"]
    standard_r = policy["standard_dp_radius"]
    extended_r = policy["extended_dp_radius"]
    drop_off_min_pts = policy["drop_off_min_pts"]
    standard_discount = policy["standard_discount"]
    extended_discount = policy["extended_discount"]

    confidence_list: list[float] = []
    for stop_lat, stop_lon in stops:
        dists = np.array(
            [haversine(p.lat, p.lon, stop_lat, stop_lon) for p in points],
            dtype=float,
        )
        immediate_mask = dists < immediate_r
        standard_mask = dists < standard_r
        extended_mask = dists < extended_r
        in_cluster = labels != -1

        immediate_count = int(immediate_mask.sum())
        standard_count = int(standard_mask.sum())
        extended_count = int(extended_mask.sum())
        immediate_stop_count = int((immediate_mask & in_cluster).sum())
        standard_stop_count = int((standard_mask & in_cluster).sum())
        extended_stop_count = int((extended_mask & in_cluster).sum())

        if immediate_count > drop_off_min_pts:
            confidence_value = immediate_stop_count / immediate_count
        elif standard_count > drop_off_min_pts:
            confidence_value = standard_stop_count / standard_count * standard_discount
        elif extended_count > drop_off_min_pts:
            confidence_value = extended_stop_count / extended_count * extended_discount
        else:
            confidence_value = 0.0

        confidence_list.append(float(confidence_value))

    visited_times = 1
    if is_round_trip:
        gap_seconds = policy["round_trip_gap_seconds"]
        pk_lat, pk_lon = stops[0]
        near_pk_times = sorted(
            parse_ts(p.ts)
            for p in points
            if haversine(p.lat, p.lon, pk_lat, pk_lon) < extended_r
        )
        for i in range(1, len(near_pk_times)):
            if (near_pk_times[i] - near_pk_times[i - 1]).total_seconds() > gap_seconds:
                visited_times += 1
        if visited_times == 1 and confidence_list:
            confidence_list[-1] /= 2

    final_confidence = mean(confidence_list) if confidence_list else 0.0

    return {
        "confidence_list": confidence_list,
        "unique_clusters": unique_clusters,
        "is_round_trip": is_round_trip,
        "pk_visited_times": visited_times,
        "final_confidence": float(final_confidence),
    }

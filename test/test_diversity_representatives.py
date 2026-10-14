"""What is kept from a cluster, per mode: centroid, medoid, extremes, quartiles."""
import numpy as np
import pytest

from amdockvs.diversity.clustering import member_rankings, pick_representatives, select_representatives


def _fps(rows: list[set[int]], nbits: int = 512) -> np.ndarray:
    x = np.zeros((len(rows), nbits), dtype=np.uint8)
    for i, bits in enumerate(rows):
        x[i, list(bits)] = 1
    return x


def test_modes_pick_the_centre_the_edges_or_a_spread():
    core = set(range(20))
    # Cluster 0: seven members drifting away from a common core, so the majority-vote centroid is
    # the middle one (row 3) and similarity to it falls off towards both ends. Cluster 1: a singleton.
    x = _fps([*(core | set(range(100, 100 + 4 * step)) for step in range(7)), set(range(300, 320))])
    labels = np.array([0] * 7 + [1])

    assert select_representatives(x, labels) == [3, 7]  # default: one, the centroid's nearest
    assert select_representatives(x, labels, per_cluster=2) == [3, 4, 7]
    # Ranked by similarity to the centroid: the farthest (0), the nearest (3), the one at the mean (5).
    assert select_representatives(x, labels, mode="extremes") == [0, 3, 5, 7]
    # One per quartile of that ranking, lowest to highest.
    assert select_representatives(x, labels, mode="quartiles") == [0, 1, 2, 3, 7]

    # Ranked by a supplied value (an activity) the picks span IT, whatever the structure says.
    activity = np.array([5.0, 9.0, 1.0, 4.0, 6.0, 7.0, 3.0, 2.0])
    assert select_representatives(x, labels, mode="extremes", values=activity) == [0, 1, 2, 7]  # min 2, mean 0, max 1
    assert select_representatives(x, labels, mode="quartiles", values=activity) == [1, 2, 3, 4, 7]
    # Unknown values are skipped; a cluster with none falls back to its centroid member.
    activity[[1, 7]] = np.nan
    assert select_representatives(x, labels, mode="extremes", values=activity) == [2, 3, 5, 7]

    with pytest.raises(ValueError):
        select_representatives(x, labels, mode="nope")


def test_picking_needs_only_the_stored_rankings_and_can_be_proportional():
    # Clusters of 20, 10 and 1 members, interleaved; the ranking is all a later pick reads.
    labels = np.array(([0, 0, 1] * 10) + [2])
    similarity = np.linspace(0.2, 0.9, len(labels)).astype(np.float32)
    for mode, sizes in {"centroid": [1, 1, 1], "quartiles": [4, 4, 1], "extremes": [3, 3, 1]}.items():
        picks = pick_representatives(labels, similarity, mode=mode)
        assert np.bincount(labels[picks]).tolist() == sizes, mode
    # 30 % of each cluster (never none): each one weighs in the result what it weighs in the run.
    picks = pick_representatives(labels, similarity, mode="proportional", fraction=0.3)
    assert np.bincount(labels[picks]).tolist() == [6, 3, 1]
    # ...spread from the lowest to the highest of the cluster.
    members = np.flatnonzero(labels == 0)
    assert {members[0], members[-1]} <= set(picks)
    # No exact centrality stored: medoid falls back to the centroid's nearest.
    nan = np.full(len(labels), np.nan)
    assert pick_representatives(labels, similarity, mode="medoid", centrality=nan) == pick_representatives(labels, similarity)


def test_rankings_are_what_the_fingerprints_say():
    x = _fps([set(range(20)), set(range(10)), set(range(20)) | {30}, set(range(100, 110))])
    similarity, centrality = member_rankings(x, np.array([0, 0, 0, 1]))
    assert np.allclose(similarity, [1.0, 0.5, 20 / 21, 1.0])  # centroid of cluster 0 = row 0
    assert np.isnan(centrality[3]) and int(np.argmax(centrality[:3])) == 0


def test_medoid_is_a_real_member_not_the_one_nearest_the_vote():
    # Three small twins, two large twins and one in between. The majority vote lands on the
    # in-between member (row 5); the medoid, closest on average to the others, is a small twin.
    small, large, between = set(range(5)), set(range(40)), set(range(20))
    x = _fps([small, small, small, large, large, between])
    labels = np.zeros(6, dtype=int)
    assert select_representatives(x, labels, mode="centroid") == [5]
    assert select_representatives(x, labels, mode="medoid") == [0]


def test_similarity_does_not_wrap_past_255_shared_bits():
    # 400 shared bits: a uint8 product reports 144 and ranks the 250-bit member first.
    x = _fps([set(range(400)), set(range(400)) | {500}, set(range(250))])
    assert select_representatives(x, np.zeros(3, dtype=int)) == [0]

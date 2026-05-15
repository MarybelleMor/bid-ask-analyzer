from app.levels import DIFF_NAMES, LEVEL_KEYS, compute_diffs, fmt


def test_level_keys_match():
    assert LEVEL_KEYS == ("1.5", "3", "5", "8", "15", "30", "60")


def test_fmt():
    assert fmt(1.5) == "1.5"
    assert fmt(3.0) == "3"
    assert fmt(60.0) == "60"


def test_diff_names_present():
    # Symmetric
    for key in LEVEL_KEYS:
        assert f"DIFF {key}" in DIFF_NAMES
    # Cross
    for name in [
        "DIFF 3B-8A",
        "DIFF 8B-3A",
        "DIFF 8B-30A",
        "DIFF 5B-15A",
        "DIFF 15B-5A",
        "DIFF 8B-15A",
        "DIFF 15B-30A",
        "DIFF 30B-15A",
    ]:
        assert name in DIFF_NAMES
    # Ring
    for name in [
        "DIFF 30-15",
        "DIFF 30-8",
        "DIFF 15-8",
        "DIFF 8-5",
    ]:
        assert name in DIFF_NAMES


def _build_bid_ask():
    bid = {"1.5": 10.0, "3": 20.0, "5": 30.0, "8": 40.0, "15": 60.0, "30": 80.0, "60": 100.0}
    ask = {"1.5": 5.0, "3": 8.0, "5": 12.0, "8": 18.0, "15": 30.0, "30": 50.0, "60": 70.0}
    return bid, ask


def test_compute_symmetric():
    bid, ask = _build_bid_ask()
    diffs = compute_diffs(bid, ask)
    assert diffs["DIFF 1.5"] == 10.0 - 5.0
    assert diffs["DIFF 8"] == 40.0 - 18.0


def test_compute_cross():
    bid, ask = _build_bid_ask()
    diffs = compute_diffs(bid, ask)
    assert diffs["DIFF 3B-8A"] == bid["3"] - ask["8"]
    assert diffs["DIFF 8B-3A"] == bid["8"] - ask["3"]


def test_compute_ring():
    bid, ask = _build_bid_ask()
    diffs = compute_diffs(bid, ask)
    # DIFF 30-15 = (BID30 - BID15) + (ASK30 - ASK15)
    assert diffs["DIFF 30-15"] == (bid["30"] - bid["15"]) + (ask["30"] - ask["15"])

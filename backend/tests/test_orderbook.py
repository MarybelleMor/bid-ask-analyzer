from app.orderbook import OrderBook


def test_apply_snapshot_and_sums():
    ob = OrderBook()
    ob.apply_snapshot({
        "lastUpdateId": 100,
        "bids": [["100.0", "1.0"], ["99.5", "2.0"], ["98.0", "5.0"]],
        "asks": [["101.0", "0.5"], ["101.5", "1.0"], ["110.0", "10.0"]],
    })
    assert ob.last_update_id == 100
    assert ob.ready
    assert ob.best_bid() == 100.0
    assert ob.best_ask() == 101.0

    mid = (100.0 + 101.0) / 2
    # within 2% on bid side covers all 3 bids (99.5 and 98 within 2% of 100.5? no, 98 is ~2.5% below)
    p = 2.0
    low = mid * (1.0 - p / 100.0)
    expected_bid = sum(px * q for px, q in [(100.0, 1.0), (99.5, 2.0), (98.0, 5.0)] if px >= low)
    assert ob.sum_bid_value(low) == expected_bid


def test_apply_diff_removes_zero_qty():
    ob = OrderBook()
    ob.apply_snapshot({
        "lastUpdateId": 100,
        "bids": [["100.0", "1.0"], ["99.5", "2.0"]],
        "asks": [["101.0", "0.5"]],
    })
    ob.apply_diff(bids=[["99.5", "0"]], asks=[["102.0", "0.7"]])
    assert ob.best_bid() == 100.0
    assert -99.5 not in ob.bids  # type: ignore[operator]
    assert 102.0 in ob.asks  # type: ignore[operator]

from daimon.adapters.slack.app import _collect_files


def test_collect_files_flattens_files_across_events_in_order():
    events = [
        {"user": "U1", "files": [{"id": "F1"}]},
        {"user": "U1"},  # no files key
        {"user": "U2", "files": [{"id": "F2"}, {"id": "F3"}]},
    ]
    ids = [f["id"] for f in _collect_files(events)]
    assert ids == ["F1", "F2", "F3"], "files are gathered across all events, order preserved"


def test_collect_files_empty_when_no_files():
    assert _collect_files([{"user": "U1"}]) == [], "no files key yields empty list"

import json
from datetime import datetime

import pytest

from kgp_opd.imedix import IST, AmbiguousDoctor, doctor_matches, find_session, roster_sessions


def ses(id_, start, session="FN", available="1", status="0", patients="3", maximum="20", before="60"):
    return {"id": id_, "session": session, "available": available, "booking_status": status,
            "patients": patients, "max_no_of_patients": maximum, "start_time": start,
            "booking_start_before": before}


ROSTER = {
    "2026-11-02": [
        {"doc_id": "doc1", "docname": "Dr. Test Doctor", "sessions": [
            ses("11", "2026-11-02 09:00:00.0"), ses("12", "2026-11-02 17:00:00.0", "AN", patients="20")]},
        {"doc_id": "doc2", "docname": "Dr. Testa Rossi", "sessions": [ses("21", "2026-11-02 10:00:00.0")]},
    ],
    "2026-11-03": [
        {"doc_id": "doc1", "docname": "Dr. Test Doctor", "sessions": [
            ses("13", "2026-11-03 09:00:00.0"), ses("14", "2026-11-03 17:00:00.0", "AN", status="2")]},
    ],
}


class FakeResponse:
    def __init__(self, data):
        self._text = json.dumps({"status": "success", "data": data})

    def text(self):
        return self._text


class FakeRequest:
    def get(self, url, params):
        if params["cmd"] == "opd-dates-current":
            return FakeResponse(sorted(ROSTER))
        return FakeResponse(ROSTER[params["rdate"]])


class FakeCtx:
    request = FakeRequest()


@pytest.mark.parametrize("query,hit", [
    ("doc1", True), ("DOC1", True), ("test doctor", True), ("Dr. Test", True), ("dr test doctor", True),
    ("doctor", True), ("rossi", False), ("", False), ("doc9", False),
])
def test_doctor_matches(query, hit):
    assert doctor_matches({"doc_id": "doc1", "docname": "Dr. Test Doctor"}, query) is hit


def test_roster_sessions_lists_everything_with_opening_times():
    s = roster_sessions(FakeCtx(), "TEST001")
    assert [x["id"] for x in s] == ["11", "21", "12", "13", "14"]
    first = s[0]
    assert first["start"] == datetime(2026, 11, 2, 9, 0, tzinfo=IST)
    assert first["opens"] == datetime(2026, 11, 2, 8, 0, tzinfo=IST)
    assert first["docname"] == "Dr. Test Doctor" and first["doc_id"] == "doc1"


def test_find_session_picks_the_soonest_bookable_one():
    assert find_session(FakeCtx(), "TEST001", None, None, "test doctor")["id"] == "11"
    # afternoon on the 2nd is full and on the 3rd is over: nothing bookable in the afternoon
    assert find_session(FakeCtx(), "TEST001", None, "AN", "doc1") is None
    assert find_session(FakeCtx(), "TEST001", "2026-11-03", None, "doc1")["id"] == "13"


def test_find_session_refuses_an_ambiguous_name():
    with pytest.raises(AmbiguousDoctor) as e:
        find_session(FakeCtx(), "TEST001", None, None, "test")
    assert "doc1" in str(e.value) and "doc2" in str(e.value)
    assert find_session(FakeCtx(), "TEST001", None, None, "doc2")["id"] == "21"

"""The synthetic demo traffic: the FakeConnector family through the real gateway and onboarding, every event marked
synthetic, nothing but schema-checked events in the output."""

import json
from datetime import UTC, datetime, timedelta

from tools.analytics.synthetic import generate, to_ndjson

END = datetime(2026, 10, 11, 21, 0, tzinfo=UTC)


def test_it_fills_every_dashboard_panel_with_synthetic_events_only():
    events = generate(families=8, days=10, seed=3, end=END)
    kinds = {e["event"] for e in events}
    assert {
        "question",
        "invite_created",
        "invite_redeemed",
        "connect_offered",
        "connect_opened",
        "login_captured",
        "kids_confirmed",
        "nudge_setting",
        "nudge",
        "session_check",
    } <= kinds
    assert all(e["synthetic"] is True and e["env"] == "dev" for e in events)
    stamps = sorted(datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) for e in events)
    assert stamps[0] >= END.replace(day=END.day - 10) and stamps[-1] <= END
    questions = [e for e in events if e["event"] == "question"]
    assert len({q["parent_ref"] for q in questions}) >= 3
    assert any(q["outcome"] == "answered" for q in questions)
    assert all(q["answer_ms"] >= 1000 for q in questions if "answer_ms" in q)


def test_same_seed_same_data_and_no_family_names_in_it():
    a = generate(families=3, days=3, seed=5, end=END)
    assert a == generate(families=3, days=3, seed=5, end=END)
    blob = json.dumps(a, ensure_ascii=False)
    for name in ("Noa", "Ido", "synthetic:", "Oranim", "Hadar"):
        assert name not in blob


def test_ndjson_rows_hold_one_event_each_for_the_bigquery_table():
    events = generate(families=2, days=2, seed=1, end=END)
    rows = to_ndjson(events).splitlines()
    assert len(rows) == len(events)
    assert json.loads(json.loads(rows[0])["payload"]) == events[0]


# ---- the data every dashboard view needs (Spec 12): skipped evenings, week-2 return or not, reconnects, nudges ----

DEMO = {"families": 12, "days": 28, "seed": 7, "end": END}


def _ts(e):
    return datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))


def _asked(events):
    return [e for e in events if e["event"] == "question" and e["outcome"] != "rate_limited"]


def _by_family(events):
    out = {}
    for e in events:
        if "parent_ref" in e:
            out.setdefault(e["parent_ref"], []).append(e)
    return out


def test_some_families_skip_evenings_and_some_almost_never():
    from tools.analytics.synthetic import ISRAEL

    events = generate(**DEMO)
    shares = []
    for evs in _by_family(events).values():
        asked = _asked(evs)
        if not asked:
            continue
        first = _ts(asked[0]).astimezone(ISRAEL).date()
        last_day = END.astimezone(ISRAEL).date()
        evenings = [
            first + timedelta(days=i)
            for i in range((last_day - first).days)
            if (first + timedelta(days=i)).isoweekday() % 7 <= 4  # Sunday (0) to Thursday (4)
        ]
        if len(evenings) < 8:
            continue
        with_tomorrow = {
            _ts(q).astimezone(ISRAEL).date() for q in asked if q.get("category") == "tomorrow"
        }
        shares.append(len(with_tomorrow & set(evenings)) / len(evenings))
    assert len(shares) >= 6
    assert min(shares) < 0.5 < max(shares)


def test_some_families_come_back_in_week_two_and_some_do_not():
    events = generate(**DEMO)
    from tools.analytics.synthetic import ISRAEL

    today = END.astimezone(ISRAEL).date()
    matured = returned = 0
    for evs in _by_family(events).values():
        answered = [e for e in evs if e["event"] == "question" and e["outcome"] == "answered"]
        if not answered:
            continue
        first = _ts(answered[0]).astimezone(ISRAEL).date()
        if first > today - timedelta(days=14):
            continue
        matured += 1
        days = {_ts(q).astimezone(ISRAEL).date() for q in _asked(evs)}
        if any(first + timedelta(days=7) <= d <= first + timedelta(days=13) for d in days):
            returned += 1
    assert matured >= 4
    assert 0 < returned < matured


def test_expired_sessions_are_followed_by_the_reconnect_button():
    events = generate(**DEMO)
    offers = [e for e in events if e["event"] == "connect_offered" and e["reason"] == "expired"]
    assert len(offers) >= 3
    by = _by_family(events)
    for offer in offers:
        before = [e for e in by[offer["parent_ref"]] if e["ts"] < offer["ts"]]
        assert any(e["event"] == "login_captured" for e in before)
        assert any(
            e["event"] == "session_expired"
            or (e["event"] == "session_check" and e["status"] == "dead")
            for e in before
        )


def test_some_nudges_are_followed_by_a_question_within_30_minutes_and_some_are_not():
    events = generate(**DEMO)
    by = _by_family(events)
    followed = ignored = 0
    for n in (e for e in events if e["event"] == "nudge" and e["result"] == "sent"):
        later = [q for q in _asked(by[n["parent_ref"]]) if n["ts"] < q["ts"]]
        if later and _ts(later[0]) - _ts(n) <= timedelta(minutes=30):
            followed += 1
        else:
            ignored += 1
    assert followed >= 3 and ignored >= 3


def test_first_answers_come_after_minutes_for_some_families_and_after_hours_for_others():
    events = generate(**DEMO)
    waits = []
    for evs in _by_family(events).values():
        offered = [e for e in evs if e["event"] == "connect_offered"]
        answered = [e for e in evs if e["event"] == "question" and e["outcome"] == "answered"]
        if offered and answered and _ts(answered[0]) >= _ts(offered[0]):
            waits.append(_ts(answered[0]) - _ts(offered[0]))
    assert len(waits) >= 5
    assert min(waits) < timedelta(minutes=30) and max(waits) > timedelta(hours=2)


def test_the_demo_data_is_still_deterministic_and_synthetic_only():
    a = generate(**DEMO)
    assert a == generate(**DEMO)
    assert all(e["synthetic"] is True for e in a)

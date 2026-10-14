"""Numbers to plain sentences (Spec 12 "Easy reading rules"). Pure functions: no pandas, no Streamlit, no BigQuery."""

from datetime import date

import pytest

from tools.dashboard import insights as i
from tools.dashboard.insights import (
    PeriodNumbers,
    ReconnectWeek,
    RouteLifetime,
    Step,
    Week2,
    WeekNumbers,
    WorthNumbers,
)

# ---------------------------------------------------------------- rounding for people


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.4, "$0.40"),
        (0.404, "$0.40"),
        (1.2, "$1.20"),
        (0.1, "$0.10"),
        (0.05, "5.0 cents"),
        (0.0034, "0.3 cents"),
        (0, "$0.00"),
    ],
)
def test_money_is_dollars_from_ten_cents_and_cents_with_one_decimal_below(value, text):
    assert i.usd(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [(9.0, "9 s"), (8.46, "8.5 s"), (0.8, "0.8 s"), (125.0, "125 s"), (9.04, "9 s")],
)
def test_seconds_have_one_decimal_unless_whole(value, text):
    assert i.seconds(value) == text


@pytest.mark.parametrize(
    ("value", "text"), [(0.7, "70%"), (0.4286, "43%"), (0.625, "63%"), (0, "0%"), (1, "100%")]
)
def test_percentages_are_whole_and_round_half_up(value, text):
    assert i.percent(value) == text


@pytest.mark.parametrize(
    ("minutes", "text"),
    [
        (0.4, "under a minute"),
        (1, "1 minute"),
        (6.4, "6 minutes"),
        (95, "1.6 hours"),
        (700, "11.7 hours"),
        (3000, "2.1 days"),
    ],
)
def test_waits_are_said_in_the_unit_people_use(minutes, text):
    assert i.minutes_text(minutes) == text


@pytest.mark.parametrize(
    ("hours", "text"),
    [(19.0, "19 h"), (7.6, "8 h"), (47, "47 h"), (120, "5 days"), (300, "12.5 days")],
)
def test_lifetimes_switch_to_days_after_two_days(hours, text):
    assert i.hours_text(hours) == text


# ---------------------------------------------------------------- comparing with the previous period


@pytest.mark.parametrize(
    ("now", "before", "q_now", "q_before", "expected"),
    [
        (0.7, 0.5, 30, 25, "up 40%, from 50%"),
        (0.3, 0.5, 30, 25, "down 40%, from 50%"),
        (0.7, 0.5, 19, 25, "too little data to compare yet"),
        (0.7, 0.5, 30, 19, "too little data to compare yet"),
        (0.7, 0.5, 20, 20, "up 40%, from 50%"),
        (0.52, 0.5, 30, 30, "about the same"),
        (0.55, 0.5, 30, 30, "about the same"),  # exactly 10%: not "more than 10%"
        (0.56, 0.5, 30, 30, "up 12%, from 50%"),
        (0.7, None, 30, 30, "too little data to compare yet"),
        (0.7, 0.0, 30, 30, "too little data to compare yet"),
        (None, 0.5, 30, 30, "too little data to compare yet"),
    ],
)
def test_a_change_is_stated_only_with_20_questions_each_and_more_than_10_percent(
    now, before, q_now, q_before, expected
):
    assert i.change(now, before, q_now, q_before) == expected


def test_a_change_in_money_shows_the_old_value_in_money():
    assert i.change(0.5, 0.4, 30, 30, show=i.usd) == "up 25%, from $0.40"


# ---------------------------------------------------------------- headline


def numbers(**kw):
    base = dict(
        active_families=2,
        school_evenings=5,
        tomorrow_pairs=7,
        coverage=0.7,
        questions=15,
        cost_per_family_month_usd=0.40,
    )
    return PeriodNumbers(**{**base, **kw})


def test_headline_two_families_is_the_spec_example():
    s = i.headline(7, numbers(), None)
    assert s.text == (
        "Last 7 days: 2 families asked about tomorrow on 7 of 10 school evenings (70%). "
        "Cost about $0.40 per family per month."
    )
    assert s.note == "Compared with the 7 days before: too little data to compare yet."
    assert not s.alert


def test_headline_one_family_says_family_and_counts():
    s = i.headline(7, numbers(active_families=1, tomorrow_pairs=3, coverage=0.6), None)
    assert s.text.startswith(
        "Last 7 days: 1 family asked about tomorrow on 3 of 5 school evenings (60%)."
    )


def test_headline_four_families_is_still_counts_not_just_a_percentage():
    s = i.headline(7, numbers(active_families=4, tomorrow_pairs=11, coverage=0.55), None)
    assert "4 families asked about tomorrow on 11 of 20 school evenings (55%)" in s.text


def test_headline_five_or_more_families_leads_with_the_share():
    s = i.headline(
        7, numbers(active_families=8, tomorrow_pairs=24, coverage=0.6, questions=80), None
    )
    assert s.text.startswith(
        "Last 7 days: 8 active families asked about tomorrow on 60% of school evenings "
        "(24 of 40 family-evenings)."
    )


def test_headline_of_an_empty_period_says_so_and_does_not_invent_numbers():
    s = i.headline(
        28,
        numbers(
            active_families=0,
            tomorrow_pairs=0,
            coverage=None,
            questions=0,
            cost_per_family_month_usd=None,
        ),
        None,
    )
    assert s.text == "Nobody asked anything in the last 28 days."
    assert s.note is None


def test_headline_with_no_rows_at_all_is_also_an_empty_period():
    assert i.headline(7, None, None).text == "Nobody asked anything in the last 7 days."


def test_headline_without_a_school_evening_in_the_period_leaves_the_coverage_out():
    s = i.headline(7, numbers(school_evenings=0, tomorrow_pairs=0, coverage=None), None)
    assert s.text == (
        "Last 7 days: 2 families asked questions, but there was no school evening to measure yet. "
        "Cost about $0.40 per family per month."
    )


def test_headline_states_the_change_when_it_is_big_enough_to_trust():
    s = i.headline(7, numbers(coverage=0.7, questions=30), numbers(coverage=0.5, questions=25))
    assert s.note == "Compared with the 7 days before: up 40%, from 50%."


def test_headline_without_a_cost_leaves_that_sentence_out():
    s = i.headline(7, numbers(cost_per_family_month_usd=None), None)
    assert "Cost" not in s.text


# ---------------------------------------------------------------- habit


def week(d, active, hist):
    return WeekNumbers(week=d, active_families=active, days_hist=hist)


@pytest.mark.parametrize(
    ("week2", "expected"),
    [
        (Week2(families=2, returned=2), "Both families came back in their second week."),
        (Week2(families=2, returned=1), "1 of 2 families came back in their second week."),
        (Week2(families=3, returned=0), "None of the 3 families came back in their second week."),
        (Week2(families=3, returned=3), "All 3 families came back in their second week."),
        (Week2(families=1, returned=1), "The family came back in its second week."),
        (Week2(families=1, returned=0), "The family did not come back in its second week."),
        (Week2(families=8, returned=5), "5 of 8 families (63%) came back in their second week."),
        (Week2(families=0, returned=0), "No family is old enough yet for the second-week check."),
        (None, "No family is old enough yet for the second-week check."),
    ],
)
def test_habit_week_two_sentence(week2, expected):
    s = i.habit([week(date(2026, 10, 4), 2, (0, 1, 1, 0, 0))], week2)
    assert s.text.startswith(expected)


def test_habit_with_no_active_weeks_says_nobody_asked():
    assert (
        i.habit([], Week2(families=2, returned=2)).text == "Nobody asked anything in this period."
    )


def test_habit_adds_the_typical_days_only_when_there_are_five_family_weeks():
    few = i.habit([week(date(2026, 10, 4), 3, (1, 1, 1, 0, 0))], None)
    assert "typical week" not in few.text
    many = i.habit(
        [week(date(2026, 9, 27), 4, (0, 1, 2, 1, 0)), week(date(2026, 10, 4), 4, (0, 0, 2, 2, 0))],
        None,
    )
    assert many.text.endswith("In a typical week a family asked on 3 days.")


# ---------------------------------------------------------------- activation

STEPS = [
    Step(1, "1 invite created", 16),
    Step(2, "2 invite redeemed", 15),
    Step(3, "3 connect button shown", 15),
    Step(4, "4 login page opened", 12),
    Step(5, "5 signed in to Webtop", 11),
    Step(6, "6 kids confirmed", 11),
    Step(7, "7 first question", 10),
    Step(8, "8 asked on 2+ days", 8),
]


def test_activation_names_the_biggest_drop_and_the_median_wait():
    s = i.activation(STEPS, median_minutes=6.2, timed_families=9)
    assert s.text == (
        "3 of 15 dropped between the connect button and opening the login page; "
        "the median parent had an answer 6 minutes after the button."
    )


def test_activation_ignores_the_second_day_step_and_takes_the_first_of_equal_drops():
    steps = [
        Step(2, "2", 10),
        Step(3, "3", 8),
        Step(4, "4", 8),
        Step(5, "5", 6),
        Step(6, "6", 6),
        Step(7, "7", 6),
    ]
    assert i.activation(steps, None, 0).text == (
        "2 of 10 dropped between redeeming the invite and the connect button."
    )


def test_activation_when_nobody_dropped():
    steps = [Step(n, str(n), 3) for n in range(2, 8)]
    s = i.activation(steps, 0.4, 3)
    assert (
        s.text
        == "All 3 got to a first answer; the median parent had an answer under a minute after the button."
    )


def test_activation_without_a_median_leaves_the_second_half_out():
    assert "median" not in i.activation(STEPS, None, 0).text


def test_activation_with_no_funnel_yet():
    assert i.activation([], None, 0).text == "Nobody has started signing up yet."


def test_activation_tolerates_a_step_that_is_bigger_than_the_one_before():
    steps = [
        Step(2, "2", 2),
        Step(3, "3", 3),
        Step(4, "4", 3),
        Step(5, "5", 3),
        Step(6, "6", 3),
        Step(7, "7", 3),
    ]
    assert i.activation(steps, None, 0).text.startswith("All 3 got to a first answer")


# ---------------------------------------------------------------- friction


def rweek(d, active, reconnects):
    return ReconnectWeek(week=d, active_families=active, reconnects=reconnects)


def test_friction_with_five_or_more_families_says_reconnects_per_family_per_week():
    weeks = [rweek(date(2026, 9, 27), 10, 10), rweek(date(2026, 10, 4), 10, 14)]
    routes = [
        RouteLifetime("code", "ended", 34, 19.0, 30.0),
        RouteLifetime("ministry", "alive", 6, 120.0, 313.0),
    ]
    s = i.friction(weeks, routes)
    assert s.text == (
        "Families reconnected 1.2 times a week. SMS-code logins last about 19 h; "
        "Ministry logins were still valid after about 5 days when last checked."
    )


def test_friction_with_few_families_says_counts():
    weeks = [rweek(date(2026, 10, 4), 2, 3)]
    assert (
        i.friction(weeks, []).text
        == "There were 3 reconnects; at most 2 families were active in a week."
    )


def test_friction_one_reconnect_one_family():
    assert i.friction([rweek(date(2026, 10, 4), 1, 1)], []).text == (
        "There was 1 reconnect; at most 1 family was active in a week."
    )


def test_friction_with_no_reconnects_says_nobody_had_to():
    assert i.friction([rweek(date(2026, 10, 4), 3, 0)], []).text == "Nobody had to reconnect."


def test_friction_route_with_both_ended_and_alive_logins_and_few_of_them():
    routes = [
        RouteLifetime("ministry", "ended", 2, 200.0, 220.0),
        RouteLifetime("ministry", "alive", 1, 100.0, 100.0),
    ]
    s = i.friction([], routes)
    assert s.text == (
        "Ministry logins last about 8.3 days (2 logins), and 1 is still valid after about 4.2 days."
    )


def test_friction_with_nothing_at_all():
    assert i.friction([], []).text == "No reconnects or logins in this period yet."


# ---------------------------------------------------------------- worth it


def worth(**kw):
    base = dict(
        questions=63,
        active_families=2,
        cost_per_family_month_usd=0.40,
        p95_seconds=9.0,
        failures=1,
        no_data=4,
    )
    return WorthNumbers(**{**base, **kw})


def test_worth_it_is_the_spec_sentence_and_goes_red_for_a_failure():
    s = i.worth_it(28, worth(), None, nudges_sent=8, nudges_followed=3)
    assert s.text == (
        "About $0.40 per family per month, 95% of answers within 9 s, 1 failure in 63 questions. "
        "4 of 63 answers needed no school data. "
        "3 of 8 evening nudges were followed by a question within 30 minutes."
    )
    assert s.alert


def test_worth_it_without_failures_is_not_red():
    s = i.worth_it(28, worth(failures=0), None, nudges_sent=0, nudges_followed=0)
    assert "no failures in 63 questions" in s.text
    assert not s.alert
    assert "nudges" not in s.text


def test_worth_it_two_failures_is_plural():
    assert "2 failures in 63 questions" in i.worth_it(28, worth(failures=2), None, 0, 0).text


def test_worth_it_of_an_empty_period():
    s = i.worth_it(
        28,
        worth(questions=0, cost_per_family_month_usd=None, p95_seconds=None, failures=0, no_data=0),
        None,
        0,
        0,
    )
    assert s.text == "No questions in the last 28 days, so there is nothing to time or price yet."
    assert not s.alert


def test_worth_it_leaves_out_what_is_missing():
    s = i.worth_it(
        7, worth(cost_per_family_month_usd=None, p95_seconds=None, no_data=0), None, 0, 0
    )
    assert s.text == "1 failure in 63 questions."


def test_worth_it_states_a_cost_change_only_when_it_can_be_trusted():
    before = worth(cost_per_family_month_usd=0.32)
    s = i.worth_it(28, worth(), before, 0, 0)
    assert s.note == "Cost per family compared with the 28 days before: up 25%, from $0.32."
    few = i.worth_it(28, worth(questions=10), before, 0, 0)
    assert few.note is None


def test_a_rate_of_exactly_one_reads_once_a_week():
    weeks = [rweek(date(2026, 9, 27), 10, 10), rweek(date(2026, 10, 4), 10, 10)]
    assert i.friction(weeks, []).text == "Families reconnected once a week."


@pytest.mark.parametrize(
    ("followed", "sent", "text"),
    [
        (1, 28, "1 of 28 evening nudges was followed by a question within 30 minutes."),
        (0, 8, "0 of 8 evening nudges were followed by a question within 30 minutes."),
        (3, 8, "3 of 8 evening nudges were followed by a question within 30 minutes."),
    ],
)
def test_the_nudge_sentence_agrees_with_its_count(followed, sent, text):
    assert i.worth_it(28, worth(), None, sent, followed).text.endswith(text)


def test_funnel_labels_are_short_enough_for_a_chart_axis():
    labels = i.funnel_labels(STEPS)
    assert labels == [
        "1 invite",
        "2 redeemed",
        "3 connect button",
        "4 login page",
        "5 signed in",
        "6 kids confirmed",
        "7 first answer",
    ], "step 8 (asked on 2+ days) is not part of the funnel chart"
    assert max(len(label) for label in labels) <= 16


def test_the_kept_line_says_what_share_got_through_each_step():
    assert i.kept_line(STEPS) == (
        "Kept from the step before: redeemed 94%, connect button 100%, login page 80%, signed in 92%, "
        "kids confirmed 100%, first answer 91%."
    )


def test_the_kept_line_never_goes_above_100_percent_or_divides_by_zero():
    steps = [Step(1, "1", 2), Step(2, "2", 3), Step(3, "3", 0), Step(4, "4", 0)]
    assert i.kept_line(steps) == (
        "Kept from the step before: redeemed 100%, connect button 0%, login page 0%."
    )
    assert i.kept_line([]) == ""

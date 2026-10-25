from app.push_alerts import failed_message, issue_counts, waiting_message, week_pair, weekly_message


def _days():
    rows = []
    for index in range(14):
        day = 28 + index
        month = 9
        year = 2026
        if day > 30:
            day -= 30
            month = 10
        clicks = 1 if index < 7 else 2
        rows.append([f"{year}-{month:02d}-{day:02d}", clicks, clicks * 10, "1%", "8.0" if index < 7 else "6.0"])
    return rows


def test_week_pair_matches_last_seven_days():
    pair = week_pair(_days())
    assert pair["current"]["clicks"] == 14
    assert pair["previous"]["clicks"] == 7
    assert pair["current"]["impressions"] == 140
    assert pair["current"]["position"] == 6
    assert pair["previous"]["position"] == 8
    assert week_pair(_days()[:7]) is None


def test_waiting_push_skips_a_notice_and_names_a_warning():
    assert waiting_message(change_id=4, name="Alt text", page="/about", level="Notice") is None
    message = waiting_message(change_id=4, name="Thin title", page="https://example.com/about", level="Warning")
    assert message["title"] == "A fix is waiting"
    assert message["url"] == "/app/queue/4"
    assert "does not publish" in message["body"]
    assert message["body"].startswith("Warning.")


def test_failed_push_says_nothing_went_live():
    message = failed_message(change_id=9, page="https://example.com/")
    assert message["title"] == "Publish failed"
    assert "Nothing went live" in message["body"]
    assert "does not retry" in message["body"]
    assert message["url"] == "/app/queue/9"


def test_weekly_push_states_both_weeks_and_does_not_promise_a_ranking():
    message = weekly_message(
        current={"clicks": 14, "impressions": 140, "position": 6},
        previous={"clicks": 7, "impressions": 70, "position": 8},
        errors=1,
        warnings=2,
        notices=3,
    )
    assert "14 this week, 7 last week" in message["body"]
    assert "1 errors, 2 warnings, 3 notices" in message["body"]
    assert "will rank" not in message["body"].lower()
    assert "guaranteed" not in message["body"].lower()
    assert message["url"] == "/app"


def test_issue_counts_keep_notices_separate():
    counts = issue_counts([
        {"severity": "Critical"},
        {"severity": "Warning"},
        {"severity": "Notice"},
        {},
    ])
    assert counts == {"errors": 1, "warnings": 1, "notices": 2}

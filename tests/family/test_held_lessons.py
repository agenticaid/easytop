"""The service hands the connector the TIME of the question, so a homework's passed due date and the lessons held
since a message can be told (today's lessons count once the school day is over). What the model gets from it is
tested in test_message_lessons_since.py."""

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.family.service import FamilyService
from tests.family.test_service import NOW


async def test_the_question_time_reaches_the_connector_so_todays_lessons_can_count():
    seen = {}

    class Spy(FakeConnector):
        async def get_homework_for_day(self, kid, day, now=None):
            seen["for_day"] = now
            return await super().get_homework_for_day(kid, day, now)

        async def get_homework_ahead(self, kid, day, horizon=7, skip_days=frozenset(), now=None):
            seen["ahead"] = now
            return await super().get_homework_ahead(kid, day, horizon, skip_days, now)

        async def get_held_lessons(self, kid, since, now):
            seen["held"] = (since, now)
            return await super().get_held_lessons(kid, since, now)

    svc = FamilyService(Spy(NOW.date(), load_synthetic()), clock=lambda: NOW)
    await svc.get_tomorrow_brief("Noa", focus="homework")
    assert seen["for_day"] == seen["ahead"] == NOW
    assert seen["held"] == (NOW.date().replace(day=28, month=9), NOW)  # seven days back, to the minute

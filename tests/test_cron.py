"""Recognising a scheduled job, and how sure the gateway is that it did.

Hermes fires no cron hook, so this has always been a question the plugin
answers for itself. What changed is what it asks: the scheduler mints a
`task_id` of `cron:<job id>:<execution id>` and binds `HERMES_CRON_SESSION` to
`"1"`, and both reach a turn hook. Core prefers the same two — its own
unattended-approval test reads that session variable, and a comment in
`tools/approval.py` says cron beats a platform marker because cron binds the
platform for delivery routing only.

The platform string stays as the last resort it always was, and a notification
now says which of the two kinds of answer it got.
"""

from hermie_plugin.push import cron
from hermie_plugin.push import events

# -- the signal --------------------------------------------------------------


def test_the_task_id_names_the_job():
    """`cron:<job id>:<execution id>` is the only place a job id reaches a hook."""
    found = cron.detect({"task_id": "cron:nightly-report:9f2c1a"})

    assert found is not None
    assert found.job_id == "nightly-report"
    assert found.source == cron.BY_TASK_ID
    assert found.certain


def test_the_session_variable_answers_when_the_task_id_does_not():
    """The test core itself uses for "is this running unattended"."""
    found = cron.detect({"task_id": "abc-123"}, session_var="1")

    assert found is not None and found.source == cron.BY_SESSION_VAR
    assert found.certain


def test_the_session_id_answers_when_neither_does():
    found = cron.detect({"session_id": "cron_nightly-report_20260922_030000"})

    assert found is not None
    assert found.job_id == "nightly-report"
    assert found.source == cron.BY_SESSION_ID


def test_a_job_id_with_an_underscore_survives_the_session_id():
    """The stamp is the last two parts, so whatever precedes them is the job."""
    found = cron.detect({"session_id": "cron_nightly_report_20260922_030000"})

    assert found is not None and found.job_id == "nightly_report"


def test_the_platform_string_is_still_read_and_is_still_a_guess():
    found = cron.detect({"platform": "cron"})

    assert found is not None and found.source == cron.BY_PLATFORM
    assert not found.certain, "a free-text field is not a fact"


def test_an_ordinary_turn_is_not_a_cron():
    for kwargs in (
        {},
        {"task_id": "5b1f", "session_id": "20260922_abcdef", "platform": "telegram"},
        {"task_id": "", "platform": ""},
    ):
        assert cron.detect(kwargs) is None


def test_an_empty_session_variable_does_not_answer():
    """The scheduler binds "" outside a run, which masks a stale environment."""
    assert cron.detect({"platform": "telegram"}, session_var="") is None


def test_a_task_id_that_merely_starts_with_the_word_is_not_one():
    assert cron.detect({"task_id": "cronies-4"}) is None


# -- the agent declaring its own failure -------------------------------------


def test_the_failure_marker_is_read_off_the_first_line_alone():
    assert cron.declared_failure("[CRON_FAILURE]\nThe API returned 503.")


def test_the_marker_mid_paragraph_declares_nothing():
    """The scheduler accepts it only alone on the first line, and so does this."""
    assert not cron.declared_failure("I considered writing [CRON_FAILURE] but it worked.")
    assert not cron.declared_failure("All good.\n[CRON_FAILURE]")


def test_nothing_declares_nothing():
    for value in (None, "", "   ", "Everything ran."):
        assert not cron.declared_failure(value)


# -- what a device is told ---------------------------------------------------


def a_cron(job_id="nightly-report", source=cron.BY_TASK_ID):
    return cron.Cron(job_id=job_id, source=source)


def test_a_delivery_carries_the_job_id_and_says_the_signal_was_a_fact():
    found = events.from_cron_delivery(
        bot="jurist", session_id="s1", turn_id="t1",
        assistant_response="Seven invoices are overdue.", at=10, cron=a_cron(),
    )

    assert found.type == "cron"
    # Carried, never shown: the contract's rule for `jobId`.
    assert "nightly-report" not in found.body
    assert "nightly-report" not in found.title
    payload = found.payload(preview=False)
    assert payload["cron"] is True and payload["cronCertain"] is True
    assert payload["jobId"] == "nightly-report"
    assert "preview" not in payload, "a payload still says who and what kind"


def test_a_platform_guess_says_so_in_the_payload():
    found = events.from_cron_delivery(
        bot="jurist", session_id="s1", turn_id="t1", assistant_response="Done.", at=10,
        cron=a_cron(job_id="", source=cron.BY_PLATFORM),
    )

    assert found.payload(preview=False)["cronCertain"] is False
    assert "jobId" not in found.payload(preview=False)


def test_a_declared_failure_is_delivered_as_a_failure():
    found = events.from_cron_delivery(
        bot="jurist", session_id="s1", turn_id="t1",
        assistant_response="[CRON_FAILURE]\nThe API returned 503.", at=10, cron=a_cron(),
    )

    assert found.type == "cron_failed"
    assert "nightly-report" not in found.body
    assert found.body == "A scheduled job failed"


def test_a_cron_turn_ending_well_is_its_own_type():
    found = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=True, failed=False, interrupted=False, at=10, cron=a_cron(),
    )

    assert found.type == "cron_done"
    assert found.payload(preview=False)["jobId"] == "nightly-report"


def test_a_cron_turn_that_failed_is_its_own_type():
    found = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=False, failed=True, interrupted=False, at=10, cron=a_cron(),
    )

    assert found.type == "cron_failed"


def test_an_ordinary_turn_still_ends_as_a_turn():
    """The two answer different questions and must not borrow each other's type."""
    done = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=True, failed=False, interrupted=False, at=10,
    )
    failed = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=False, failed=True, interrupted=False, at=10,
    )

    assert (done.type, failed.type) == ("turn_done", "turn_failed")
    assert done.payload(preview=False).get("cron") is None


def test_an_interrupted_cron_turn_still_says_nothing():
    """Somebody pressed stop, which is as true of a cron run as of a chat."""
    assert events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=False, failed=False, interrupted=True, at=10, cron=a_cron(),
    ) is None


def test_the_two_descriptions_of_one_failed_run_buzz_once():
    """The agent's own marker and the turn ending both describe one fact."""
    declared = events.from_cron_delivery(
        bot="jurist", session_id="s1", turn_id="t1",
        assistant_response="[CRON_FAILURE]\nnope", at=10, cron=a_cron(),
    )
    ended = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=False, failed=True, interrupted=False, at=11, cron=a_cron(),
    )

    assert declared.event_id == ended.event_id


def test_a_finished_cron_and_a_finished_turn_do_not_collide():
    """Different facts about one turn id must keep different ids."""
    scheduled = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=True, failed=False, interrupted=False, at=10, cron=a_cron(),
    )
    ordinary = events.from_session_end(
        bot="jurist", session_id="s1", turn_id="t1",
        completed=True, failed=False, interrupted=False, at=10,
    )

    assert scheduled.event_id != ordinary.event_id


# -- suppression -------------------------------------------------------------


def test_a_failed_job_is_never_suppressed():
    """Nobody asked for the run, so nobody is waiting to notice it did not work."""
    assert "cron_failed" in events.NEVER_SUPPRESSED
    assert "cron" in events.NEVER_SUPPRESSED


def test_a_finished_job_is_suppressed_like_any_other_good_news():
    """A device that says it is reading that chat is already looking at it."""
    assert "cron_done" not in events.NEVER_SUPPRESSED


# -- through the module, the way a gateway drives it -------------------------


class FakeRuntime:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return "jurist"


def push_module(settings=None):
    from hermie_plugin.push import PushModule

    module = PushModule(FakeRuntime(settings))
    offered = []
    module.offer = lambda notification, delay=False: offered.append(notification)
    return module, offered


def test_the_hook_turns_a_cron_delivery_into_a_cron_notification():
    module, offered = push_module()

    module.on_post_llm_call(
        session_id="s1", turn_id="t1", task_id="cron:nightly-report:9f2c",
        assistant_response="Seven invoices are overdue.", platform="",
    )

    assert [n.type for n in offered] == ["cron"]
    assert offered[0].payload(preview=False)["jobId"] == "nightly-report"


def test_the_hook_reads_the_session_variable_when_the_task_id_is_silent(monkeypatch):
    """The gateway that binds the marker but hands the hook an opaque task id."""
    from hermie_plugin.push import cron as cron_module

    monkeypatch.setattr(cron_module, "read_session_var", lambda: "1")
    module, offered = push_module()

    module.on_post_llm_call(
        session_id="20260922_abcdef", turn_id="t1", task_id="5b1f",
        assistant_response="Seven invoices are overdue.", platform="telegram",
    )

    assert [n.type for n in offered] == ["cron"]
    assert offered[0].payload(preview=False)["cronCertain"] is True


def test_an_ordinary_message_is_untouched_by_any_of_this():
    module, offered = push_module()

    module.on_post_llm_call(
        session_id="s1", turn_id="t1", task_id="5b1f",
        assistant_response="Here you go.", platform="telegram",
    )

    assert [n.type for n in offered] == ["message"]
    assert "cron" not in offered[0].payload(preview=False)


def test_a_cron_turn_ending_goes_through_the_hook_too():
    module, offered = push_module()

    module.on_session_end(
        session_id="s1", turn_id="t1", task_id="cron:nightly-report:9f2c",
        completed=True, failed=False, interrupted=False,
    )

    assert [n.type for n in offered] == ["cron_done"]


def test_the_new_types_are_on_by_default_and_switchable():
    module, _ = push_module()
    assert "cron_done" in module.enabled_types and "cron_failed" in module.enabled_types

    narrowed, _ = push_module({"push.types": ["message", "cron"]})
    assert "cron_done" not in narrowed.enabled_types
    assert "push.type.cron_done" not in narrowed.capabilities()


# -- the cron fields, on every payload that can carry them -------------------


def test_every_cron_notification_says_how_sure_the_gateway_is():
    """`cronCertain` is the difference between a fact and a guess about a
    free-text platform string, and an app that labels a notification
    "scheduled job" is relying on it."""
    certain = a_cron()
    guessed = cron.Cron(source=cron.BY_PLATFORM)

    for built in (
        events.from_cron_delivery(
            bot="b", session_id="s", turn_id="t", assistant_response="done", at=10, cron=certain
        ),
        events.from_session_end(
            bot="b", session_id="s", turn_id="t", completed=True, failed=False,
            interrupted=False, at=10, cron=certain,
        ),
        events.from_session_end(
            bot="b", session_id="s", turn_id="t", completed=False, failed=True,
            interrupted=False, at=10, cron=certain,
        ),
    ):
        payload = built.payload(preview=False)
        assert payload["cron"] is True
        assert payload["cronCertain"] is True
        assert payload["jobId"] == "nightly-report"

    payload = events.from_session_end(
        bot="b", session_id="s", turn_id="t", completed=True, failed=False,
        interrupted=False, at=10, cron=guessed,
    ).payload(preview=False)
    assert payload["cronCertain"] is False
    # A job id is only ever said when one was actually read.
    assert "jobId" not in payload


def test_an_approval_raised_inside_a_scheduled_run_says_so():
    """It is still a request — somebody is being asked — but "this is a job you
    are not watching" is the most useful thing a lock screen can add."""
    payload = events.from_approval(
        bot="b", session_key="cron_nightly-report_20260922_030000", description="d",
        request_id="r1", turn_id="t1", at=10, cron=a_cron(),
    ).payload(preview=False)

    assert payload["type"] == "request"
    assert payload["cron"] is True and payload["cronCertain"] is True
    assert payload["jobId"] == "nightly-report"
    assert payload["requestId"] == "r1"


def test_a_question_asked_inside_a_scheduled_run_says_so_too():
    payload = events.from_clarify(
        bot="b", session_id="cron_nightly-report_20260922_030000", tool_call_id="c1",
        question="which quarter?", at=10, cron=a_cron(),
    ).payload(preview=False)

    assert payload["type"] == "request"
    assert payload["cron"] is True and payload["jobId"] == "nightly-report"


def test_an_ordinary_request_carries_no_cron_fields():
    payload = events.from_approval(
        bot="b", session_key="s1", description="d", request_id="r1", turn_id="t1", at=10
    ).payload(preview=False)

    assert "cron" not in payload and "cronCertain" not in payload


def test_the_approval_hook_reads_the_session_variable_for_its_answer(monkeypatch):
    """`pre_approval_request` carries no `task_id`, so the marker core binds for
    the run is what is left — and core fills in `session_id` on the hook."""
    from hermie_plugin.push import cron as cron_module

    monkeypatch.setattr(cron_module, "read_session_var", lambda: "1")
    module, offered = push_module()

    module.on_pre_approval_request(
        surface="gateway", session_key="s1", session_id="s1", description="delete it",
        request_id="r1", turn_id="t1",
    )

    assert [n.type for n in offered] == ["request"]
    assert offered[0].payload(preview=False)["cronCertain"] is True


def test_the_clarify_hook_reads_the_task_id_for_its_answer():
    module, offered = push_module()

    module.on_pre_tool_call(
        tool_name="clarify", session_id="s1", task_id="cron:nightly-report:9f2c",
        tool_call_id="c1", args={"question": "which quarter?"},
    )

    assert [n.type for n in offered] == ["request"]
    assert offered[0].payload(preview=False)["jobId"] == "nightly-report"

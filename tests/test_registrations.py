"""The registration reader, which is the one place a bad bag of JSON arrives."""

from hermie_plugin.push.registrations import (
    Section,
    Seen,
    is_muted,
    looking_at,
    mutes_of,
    read_section,
    read_sections,
    registration_of,
    seen_of,
)


def expo_entry(**overrides):
    entry = {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "ios",
        "types": {"message": True, "request": True},
        "preview": False,
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


def test_a_good_expo_entry_reads():
    parsed = registration_of("i1", expo_entry())
    assert parsed is not None
    assert parsed.transport == "expo"
    assert parsed.wants("message") is True


def test_an_absent_type_is_off():
    """A device that has never heard of a type cannot have agreed to it."""
    parsed = registration_of("i1", expo_entry())
    assert parsed.wants("turn_done") is False


def test_an_unknown_version_is_dropped():
    assert registration_of("i1", expo_entry(v=2)) is None


def test_an_entry_carrying_both_transports_is_a_confusion():
    both = expo_entry(endpoint="https://push.example/x")
    assert registration_of("i1", both) is None


def test_a_webpush_entry_needs_both_keys():
    base = {
        "v": 1,
        "transport": "webpush",
        "endpoint": "https://push.example/x",
        "platform": "web",
        "types": {"message": True},
        "updatedAt": 1,
    }
    assert registration_of("i1", {**base, "keys": {"p256dh": "a"}}) is None
    assert registration_of("i1", {**base, "keys": {"p256dh": "a", "auth": "b"}}) is not None


def test_one_bad_entry_costs_only_itself():
    section = read_section(
        {"push": {"registrations": {"good": expo_entry(), "bad": {"v": 1, "transport": "carrier-pigeon"}}}}
    )
    assert [entry.installation_id for entry in section.registrations] == ["good"]


def test_the_section_survives_anything():
    for junk in (None, 42, "push", {}, {"push": "yes"}, {"push": {"registrations": []}}):
        assert read_section(junk).registrations == []


def test_seen_is_a_heartbeat_with_a_window():
    section = Section(seen={"i1": Seen(at=1000, bot="jurist")})
    assert looking_at(section, "i1", "jurist", now=1050, window_seconds=90) is True
    assert looking_at(section, "i1", "jurist", now=1200, window_seconds=90) is False


def test_a_heartbeat_is_about_one_device_and_one_chat():
    section = Section(seen={"i1": Seen(at=1000, bot="jurist")})
    assert looking_at(section, "i1", "marketing", now=1050, window_seconds=90) is False
    assert looking_at(section, "i2", "jurist", now=1050, window_seconds=90) is False


def test_a_heartbeat_that_names_no_bot_still_covers_every_chat():
    """The older shape said a chat was open without saying which."""
    section = Section(seen={"i1": Seen(at=1000)})
    assert looking_at(section, "i1", "anything", now=1050, window_seconds=90) is True


def test_both_heartbeat_shapes_read():
    assert seen_of(1000) == Seen(at=1000, bot="")
    assert seen_of({"bot": "jurist", "at": 1000}) == Seen(at=1000, bot="jurist")
    for junk in (None, 0, -5, "now", True, {}, {"bot": "jurist"}, {"at": "now"}):
        assert seen_of(junk) is None


def test_a_heartbeat_can_ride_the_registration_entry():
    section = read_section(
        {"push": {"registrations": {"i1": expo_entry(seen={"bot": "jurist", "at": 1000})}}}
    )
    assert section.seen == {"i1": Seen(at=1000, bot="jurist")}


def test_the_newest_heartbeat_wins():
    """"Is somebody looking now" has one right answer, and it is the latest one."""
    section = read_section(
        {
            "push": {
                "registrations": {"i1": expo_entry(seen={"bot": "jurist", "at": 1000})},
                "seen": {"i1": {"bot": "marketing", "at": 2000}},
            }
        }
    )
    assert section.seen["i1"] == Seen(at=2000, bot="marketing")

    across_keys = read_sections(
        [
            ("", {"push": {"seen": {"i1": {"bot": "marketing", "at": 2000}}}}),
            ("u1", {"push": {"seen": {"i1": {"bot": "jurist", "at": 1000}}}}),
        ]
    )
    assert across_keys.seen["i1"] == Seen(at=2000, bot="marketing")


# -- one key per person ------------------------------------------------------


def bag(*installation_ids, seen=None):
    return {"push": {"registrations": {i: expo_entry() for i in installation_ids}, "seen": seen or {}}}


def test_a_registration_knows_whose_key_it_came_from():
    section = read_sections([("u1", bag("i1"))])
    assert section.registrations[0].user_id == "u1"


def test_the_legacy_key_names_nobody():
    section = read_sections([("", bag("i1"))])
    assert section.registrations[0].user_id == ""


def test_both_keys_are_read_and_the_devices_add_up():
    section = read_sections([("", bag("i1")), ("u1", bag("i2")), ("u2", bag("i3"))])
    assert [(r.installation_id, r.user_id) for r in section.registrations] == [
        ("i1", ""), ("i2", "u1"), ("i3", "u2")
    ]


def test_the_per_user_key_wins_for_the_same_device():
    """While the app writes both, the device must not be notified twice."""
    section = read_sections([("", bag("i1")), ("u1", bag("i1"))])
    assert len(section.registrations) == 1
    assert section.registrations[0].user_id == "u1"


def test_a_missing_per_user_key_costs_nothing():
    assert read_sections([]).registrations == []
    assert read_sections([("u1", None), ("u2", {"push": "yes"})]).registrations == []


# -- the mute list -----------------------------------------------------------


def test_a_mute_list_reads_from_the_top_of_the_bag():
    section = read_sections([("u1", {"mutes": {"jurist": 0, "marketing": 1790000000}})])
    assert section.mutes == {"u1": {"jurist": 0, "marketing": 1790000000}}


def test_a_mute_list_filed_under_push_is_honoured_too():
    assert mutes_of({"push": {"mutes": {"jurist": 0}}}) == {"jurist": 0}


def test_the_top_level_mute_list_wins_where_they_disagree():
    assert mutes_of({"mutes": {"jurist": 0}, "push": {"mutes": {"jurist": 5}}}) == {"jurist": 0}


def test_a_mute_that_is_not_a_number_is_not_a_mute():
    assert mutes_of({"mutes": {"jurist": "forever", "marketing": True, "sales": -1}}) == {}


def test_is_muted_reads_zero_as_forever_and_a_past_until_as_over():
    section = Section(mutes={"u1": {"jurist": 0, "marketing": 900}})
    assert is_muted(section, "u1", "jurist", now=99_999_999) is True
    assert is_muted(section, "u1", "marketing", now=1000) is False
    assert is_muted(section, "u1", "sales", now=1000) is False
    assert is_muted(section, "u2", "jurist", now=1000) is False


def test_there_is_no_type_for_something_nothing_can_send():
    """Hermes fires no hook when one bot writes to another."""
    from hermie_plugin.push import events
    from hermie_plugin.push.registrations import PUSH_TYPES

    assert "dm" not in PUSH_TYPES
    assert "dm" not in events.TYPES
    # A device that still asks for it is simply asking for nothing.
    assert registration_of("i1", expo_entry(types={"dm": True})).wants("dm") is False


# -- which VAPID key a browser subscription was made with --------------------

# An uncompressed P-256 point in base64url is 87 characters; the reader takes
# 80 to 100 so a key it cannot judge is never mistaken for a different one.
A_KEY = "B" + "x" * 86


def webpush_entry(**overrides):
    entry = {
        "v": 1,
        "transport": "webpush",
        "endpoint": "https://push.example/x",
        "keys": {"p256dh": "a", "auth": "b"},
        "platform": "web",
        "types": {"message": True},
        "updatedAt": 1,
    }
    entry.update(overrides)
    return entry


def test_a_webpush_row_can_say_which_key_it_was_made_with():
    assert registration_of("i1", webpush_entry(applicationServerKey=A_KEY)).application_server_key == A_KEY


def test_a_row_that_says_nothing_about_its_key_reads_as_unknown():
    assert registration_of("i1", webpush_entry()).application_server_key == ""


def test_a_key_that_is_not_base64url_of_the_right_length_reads_as_absent():
    for bad in (
        "x" * 79,
        "x" * 101,
        A_KEY[:-1] + "=",
        A_KEY[:-1] + "+",
        A_KEY[:-1] + "/",
        " " + A_KEY[1:],
        A_KEY + "\n",
        87,
        None,
        ["x" * 87],
    ):
        registration = registration_of("i1", webpush_entry(applicationServerKey=bad))
        # Absent, never a reason to drop the row: it is tried as before.
        assert registration is not None
        assert registration.application_server_key == ""
    assert registration_of("i1", webpush_entry(applicationServerKey="x" * 80)).application_server_key == "x" * 80
    assert registration_of("i1", webpush_entry(applicationServerKey="x" * 100)).application_server_key == "x" * 100


def test_only_a_webpush_row_carries_a_key():
    assert registration_of("i1", expo_entry(applicationServerKey=A_KEY)).application_server_key == ""

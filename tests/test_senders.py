"""The two transports, driven without a network."""

import json

import pytest

from hermie_plugin.push import expo, webpush


# -- Expo --------------------------------------------------------------------


def test_a_mangled_token_is_caught_before_a_round_trip():
    assert expo.is_expo_token("ExponentPushToken[abc]") is True
    assert expo.is_expo_token("ExpoPushToken[abc]") is True
    assert expo.is_expo_token("not-a-token") is False
    assert expo.is_expo_token("") is False


def test_tickets_line_up_with_the_messages_that_produced_them():
    def post(url, body):
        return {"data": [{"status": "ok", "id": "r1"}, {"status": "ok", "id": "r2"}]}

    tickets = expo.send(
        [{"to": "ExponentPushToken[a]"}, {"to": "ExponentPushToken[b]"}], post=post
    )
    assert [t.token for t in tickets] == ["ExponentPushToken[a]", "ExponentPushToken[b]"]
    assert [t.receipt_id for t in tickets] == ["r1", "r2"]


def test_a_dead_device_is_recognised_from_a_ticket():
    def post(url, body):
        return {"data": [{"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}]}

    ticket = expo.send([{"to": "ExponentPushToken[a]"}], post=post)[0]
    assert ticket.device_gone is True


def test_a_refused_batch_retires_nobody():
    """A request that failed says nothing about whether a device still exists."""

    def post(url, body):
        raise OSError("network is down")

    tickets = expo.send([{"to": "ExponentPushToken[a]"}], post=post)
    assert tickets[0].status == "error"
    assert tickets[0].device_gone is False


def test_a_body_without_tickets_is_not_read_as_success():
    def post(url, body):
        return {"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS"}]}

    tickets = expo.send([{"to": "ExponentPushToken[a]"}], post=post)
    assert tickets[0].status == "error"
    assert tickets[0].device_gone is False


def test_receipts_that_have_no_answer_yet_are_simply_absent():
    def post(url, body):
        return {"data": {"r1": {"status": "ok"}}}

    found = expo.receipts(["r1", "r2"], post=post)
    assert set(found) == {"r1"}


def test_the_message_carries_the_payload_and_the_two_visible_strings():
    message = expo.message_for(
        "ExponentPushToken[a]", {"type": "request", "bot": "jurist"}, title="jurist", body="Needs your approval"
    )
    assert message["to"] == "ExponentPushToken[a]"
    assert message["data"]["type"] == "request"
    assert message["channelId"] == "request"
    assert message["title"] == "jurist"


def test_a_batch_over_the_limit_is_refused_rather_than_silently_cut():
    with pytest.raises(ValueError):
        expo.send([{"to": "x"}] * (expo.MAX_BATCH + 1), post=lambda url, body: {"data": []})


# -- Web Push ----------------------------------------------------------------

cryptography = pytest.importorskip("cryptography")


def subscription():
    """A browser's half of the exchange, generated the way a browser would."""
    import os

    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key, webpush.b64(webpush.public_key_bytes(key)), webpush.b64(os.urandom(16))


def test_base64url_round_trips_without_padding():
    sample = bytes([0, 255, 16])
    assert webpush.unb64(webpush.b64(sample)) == sample
    assert "=" not in webpush.b64(b"abc")


def test_a_key_is_minted_once_and_then_reused(tmp_path):
    path = tmp_path / "vapid.pem"
    first = webpush.load_or_create_key(path)
    again = webpush.load_or_create_key(path)
    assert webpush.public_key_bytes(first) == webpush.public_key_bytes(again)
    assert path.stat().st_mode & 0o777 == 0o600


def test_an_unreadable_key_is_not_overwritten(tmp_path):
    """Overwriting would make every push service treat this sender as new."""
    path = tmp_path / "vapid.pem"
    path.write_text("this is not a PEM file")
    with pytest.raises(Exception):
        webpush.load_or_create_key(path)
    assert path.read_text() == "this is not a PEM file"


def test_the_vapid_header_is_scoped_to_one_push_service(tmp_path):
    import base64

    key = webpush.load_or_create_key(tmp_path / "vapid.pem")
    header = webpush.vapid_header(key, "https://updates.push.services.mozilla.com/wpush/v2/abc", "mailto:a@b.c")

    assert header.startswith("vapid t=")
    token = header[len("vapid t=") :].split(",")[0]
    claims = json.loads(webpush.unb64(token.split(".")[1]))
    assert claims["aud"] == "https://updates.push.services.mozilla.com"
    assert claims["sub"] == "mailto:a@b.c"
    assert claims["exp"] > 0


def test_a_payload_encrypts_and_the_browser_can_open_it(tmp_path):
    """The whole RFC 8291 derivation, checked by decrypting it back."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    user_key, p256dh, auth = subscription()
    body = webpush.encrypt(b'{"type":"request"}', p256dh, auth)

    salt, record_size, id_len = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    sender_public = body[21 : 21 + id_len]
    ciphertext = body[21 + id_len :]
    assert record_size == webpush.RECORD_SIZE
    assert id_len == 65

    # Replay the derivation from the receiving side.
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public)
    shared = user_key.exchange(ec.ECDH(), sender)
    key_info = b"WebPush: info\x00" + webpush.public_key_bytes(user_key) + sender_public
    ikm = webpush._hkdf(webpush.unb64(auth), shared, key_info, 32)
    content_key = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    plaintext = AESGCM(content_key).decrypt(nonce, ciphertext, None)
    assert plaintext == b'{"type":"request"}\x02'


def test_every_message_gets_fresh_material(tmp_path):
    _, p256dh, auth = subscription()
    first = webpush.encrypt(b"x", p256dh, auth)
    again = webpush.encrypt(b"x", p256dh, auth)
    assert first[:16] != again[:16]  # salt
    assert first[21:86] != again[21:86]  # ephemeral public key


def test_gone_is_gone_and_a_blip_is_not(tmp_path):
    key = webpush.load_or_create_key(tmp_path / "vapid.pem")
    _, p256dh, auth = subscription()

    def answering(status):
        def request(url, body, headers):
            assert headers["Content-Encoding"] == "aes128gcm"
            assert headers["Authorization"].startswith("vapid t=")
            return status, ""

        return request

    assert webpush.send(key, "https://push.example/x", p256dh, auth, {}, request=answering(201)).ok is True
    assert webpush.send(key, "https://push.example/x", p256dh, auth, {}, request=answering(410)).device_gone is True
    assert webpush.send(key, "https://push.example/x", p256dh, auth, {}, request=answering(500)).device_gone is False


def test_a_key_that_appears_while_minting_is_kept_rather_than_overwritten(tmp_path, monkeypatch):
    """Two processes loading at once (a gateway and a `validate` probe) end up with one key.

    The key is minted at load now, so the window between "there is no file" and
    "write one" is crossed by every process that loads the plugin. The second
    writer must read the first one's key, never replace it: a replaced key is a
    gateway every browser subscription stops matching.
    """
    path = tmp_path / "vapid.pem"
    first = webpush.load_or_create_key(path)
    original = path.read_bytes()

    real_exists = type(path).exists
    monkeypatch.setattr(type(path), "exists", lambda self: False if self == path else real_exists(self))
    again = webpush.load_or_create_key(path)

    assert path.read_bytes() == original
    assert webpush.public_key_bytes(again) == webpush.public_key_bytes(first)


def test_the_public_key_is_the_uncompressed_point_in_base64url(tmp_path):
    key = webpush.load_or_create_key(tmp_path / "vapid.pem")
    text = webpush.public_key_b64(key)

    assert len(text) == 87
    assert "=" not in text
    assert webpush.unb64(text) == webpush.public_key_bytes(key)
    assert webpush.unb64(text)[0] == 4


# What the push services answer when a subscription was made with another key.
APPLE_MISMATCH = '{"reason":"VapidPkHashMismatch"}'
FCM_MISMATCH = (
    "the key in the authorization header does not correspond to the sender ID used to subscribe this user"
)
FCM_MISMATCH_NEW = (
    "the VAPID credentials in the authorization header do not correspond to the credentials used to "
    "create the subscriptions."
)


def test_only_a_403_that_says_so_is_a_key_mismatch():
    for body in (APPLE_MISMATCH, FCM_MISMATCH, FCM_MISMATCH_NEW):
        assert webpush.Result(status=403, error=body).key_mismatch is True
        assert webpush.Result(status=401, error=body).key_mismatch is False
    # A 403 for a token the service will not take is not about the key.
    assert webpush.Result(status=403, error='{"reason":"BadJwtToken"}').key_mismatch is False
    assert webpush.Result(status=403).key_mismatch is False
    assert webpush.Result(status=403).forbidden is True
    for status in (201, 400, 401, 404, 410, 413, 429, 500, 0):
        assert webpush.Result(status=status).key_mismatch is False
        assert webpush.Result(status=status).forbidden is False
    assert webpush.Result(status=403).device_gone is False


def test_minting_writes_the_whole_file_or_none_at_all(tmp_path, monkeypatch):
    """A key file that exists is a finished one: a failed write leaves nothing to refuse later."""
    import os

    path = tmp_path / "vapid.pem"

    def broken(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken)
    with pytest.raises(OSError):
        webpush.load_or_create_key(path)
    assert sorted(item.name for item in tmp_path.iterdir()) == []

    monkeypatch.undo()
    key = webpush.load_or_create_key(path)
    assert webpush.public_key_bytes(webpush.load_or_create_key(path)) == webpush.public_key_bytes(key)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["vapid.pem"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_lost_race_reads_the_winners_key_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    import os

    path = tmp_path / "vapid.pem"
    winner = webpush.load_or_create_key(tmp_path / "winner.pem")
    real_link = os.link

    def link_after_the_winner(source, target, *args, **kwargs):
        # The other process got there first, between our check and our link.
        (tmp_path / "winner.pem").rename(path)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", link_after_the_winner)
    got = webpush.load_or_create_key(path)

    assert webpush.public_key_bytes(got) == webpush.public_key_bytes(winner)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["vapid.pem"]


def test_without_hard_links_the_key_is_still_created_exclusively(tmp_path, monkeypatch):
    import errno
    import os

    def no_links(*_args, **_kwargs):
        raise OSError(errno.EPERM, "no hard links on this file system")

    monkeypatch.setattr(os, "link", no_links)
    path = tmp_path / "vapid.pem"
    key = webpush.load_or_create_key(path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == ["vapid.pem"]
    monkeypatch.undo()
    assert webpush.public_key_bytes(webpush.load_or_create_key(path)) == webpush.public_key_bytes(key)


def test_loading_without_minting_finds_nothing_and_creates_nothing(tmp_path):
    path = tmp_path / "vapid.pem"
    assert webpush.load_or_create_key(path, create=False) is None
    assert not path.exists()
    key = webpush.load_or_create_key(path)
    assert webpush.public_key_bytes(webpush.load_or_create_key(path, create=False)) == webpush.public_key_bytes(key)


# -- the key this gateway publishes, and the rows that name one --------------
#
# HERM-152: the plugin minted a VAPID key of its own and never said what it was,
# so a browser subscribed with another sender's key and every push from here
# was refused by the push service, quietly. The key is now in the advert, a row
# can say which key its subscription was made with, and a refusal for the wrong
# key retires the row instead of failing on every notification.

import time  # noqa: E402

import yaml  # noqa: E402

from test_plugin import app_meta_with, gateway  # noqa: E402

import hermie_plugin  # noqa: E402
import hermie_plugin.push as push_pkg  # noqa: E402
from hermie_plugin import contract, uimeta  # noqa: E402
from hermie_plugin.push import events  # noqa: E402


def webpush_row(**overrides):
    entry = {
        "v": 1,
        "transport": "webpush",
        "endpoint": "https://push.example/w1",
        "keys": {"p256dh": "p", "auth": "a"},
        "platform": "web",
        "types": {"request": True},
        "preview": False,
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


def loaded_push(tmp_path, monkeypatch, rows=None, settings=None):
    """The push module as `register` leaves it, over a profile holding *rows*."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations=rows or {}), settings=settings)
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    module = push_pkg.register(ctx, hermie_plugin.Runtime(ctx, home=home))
    return home, module


def push_service(monkeypatch, status=201, error=""):
    """Every Web Push send answers *status*; returns the endpoints that were asked."""
    asked = []

    def send(key, endpoint, p256dh, auth, payload, contact=""):
        asked.append(endpoint)
        return webpush.Result(status=status, error=error)

    monkeypatch.setattr(push_pkg.webpush, "send", send)
    return asked


def approval(number):
    return events.from_approval(
        bot="jurist", session_key="s1", description="d", request_id=f"r{number}", turn_id="t", at=10
    )


def rewrite_rows(home, rows):
    document = yaml.safe_load((home / "profile.yaml").read_text())
    document["ui_meta"]["hermie-app"] = app_meta_with(registrations=rows)
    (home / "profile.yaml").write_text(yaml.safe_dump(document))


def test_the_advert_publishes_the_key_this_gateway_signs_with(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    # Minted at load, not on the first send: a client needs it before anything
    # has ever been sent to it.
    path = ctx.state.data_dir / "vapid.pem"
    assert path.is_file()
    signing = webpush.load_or_create_key(path)
    assert advert["webPush"] == {"publicKey": webpush.public_key_b64(signing)}
    caps = contract.read_capabilities(advert)
    assert contract.CAP_PUSH_WEBPUSH in caps
    assert contract.CAP_PUSH_WEBPUSH_KEY in caps


def test_the_published_key_survives_a_restart(tmp_path, monkeypatch):
    """Never rotated: a second load publishes the key the first one minted."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)
    first = uimeta.read_key(uimeta.PLUGIN_KEY, home)["webPush"]["publicKey"]
    hermie_plugin.register(ctx)

    assert uimeta.read_key(uimeta.PLUGIN_KEY, home)["webPush"]["publicKey"] == first


def test_without_the_signing_library_there_is_no_key_and_no_claim(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    monkeypatch.setattr(push_pkg.webpush, "available", lambda: False)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert "webPush" not in advert
    caps = contract.read_capabilities(advert)
    assert contract.CAP_PUSH_WEBPUSH not in caps
    assert contract.CAP_PUSH_WEBPUSH_KEY not in caps
    assert not (ctx.state.data_dir / "vapid.pem").exists()


def test_an_unreadable_key_is_not_published_and_not_overwritten(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    path = ctx.state.data_dir / "vapid.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a PEM file")

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert "webPush" not in advert
    assert contract.CAP_PUSH_WEBPUSH_KEY not in contract.read_capabilities(advert)
    assert path.read_text() == "this is not a PEM file"


def test_push_switched_off_publishes_no_key_and_mints_none(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(), settings={"modules.push": False})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert "webPush" not in advert
    assert contract.CAP_PUSH_WEBPUSH_KEY not in contract.read_capabilities(advert)
    assert not (ctx.state.data_dir / "vapid.pem").exists()


def test_a_row_that_names_no_key_is_tried(tmp_path, monkeypatch):
    """Every row written before rows said which key they used."""
    _, module = loaded_push(tmp_path, monkeypatch, {"w1": webpush_row()})
    asked = push_service(monkeypatch)

    assert module.deliver(approval(1)) == 1
    assert asked == ["https://push.example/w1"]


def test_a_row_that_names_this_gateways_key_is_sent(tmp_path, monkeypatch):
    home, module = loaded_push(tmp_path, monkeypatch)
    rewrite_rows(home, {"w1": webpush_row(applicationServerKey=module.web_push_public_key())})
    asked = push_service(monkeypatch)

    assert module.deliver(approval(1)) == 1
    assert asked == ["https://push.example/w1"]


def test_a_row_that_names_another_key_is_skipped_and_said_once(tmp_path, monkeypatch, caplog):
    other = webpush.public_key_b64(webpush.load_or_create_key(tmp_path / "other.pem"))
    home, module = loaded_push(tmp_path, monkeypatch)
    assert other != module.web_push_public_key()
    rewrite_rows(
        home,
        {
            "w1": webpush_row(applicationServerKey=other),
            "w2": webpush_row(endpoint="https://push.example/w2"),
        },
    )
    asked = push_service(monkeypatch)

    with caplog.at_level("WARNING"):
        assert module.deliver(approval(1)) == 1
        assert module.deliver(approval(2)) == 1

    # Only the row that names no key was asked; the other was never posted,
    # because the push service would refuse it anyway.
    assert asked == ["https://push.example/w2", "https://push.example/w2"]
    said = [record for record in caplog.records if "w1" in record.getMessage()]
    assert len(said) == 1
    # A mismatch is not a retirement: the device fixes it by subscribing again.
    assert module.runtime.state.is_retired("w1", updated_at=1789957143) is False


def test_mismatch_reports_are_capped(tmp_path, monkeypatch, caplog):
    other = webpush.public_key_b64(webpush.load_or_create_key(tmp_path / "other.pem"))
    rows = {f"w{index}": webpush_row(applicationServerKey=other) for index in range(push_pkg.MAX_REPORTS + 10)}
    _, module = loaded_push(tmp_path, monkeypatch, rows)
    push_service(monkeypatch)

    with caplog.at_level("WARNING"):
        assert module.deliver(approval(1)) == 0

    assert len([record for record in caplog.records if "applicationServerKey" in record.getMessage()]) == push_pkg.MAX_REPORTS


def test_a_403_retires_the_row_until_the_device_writes_it_again(tmp_path, monkeypatch):
    home, module = loaded_push(tmp_path, monkeypatch, {"w1": webpush_row()})
    asked = push_service(monkeypatch, status=403)

    assert module.deliver(approval(1)) == 0
    assert asked == ["https://push.example/w1"]
    retired = module.runtime.state.data["retired"]["w1"]
    assert retired["reason"] == "webpush-key"
    # Kept in the plugin's own state; the app's row is never touched.
    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert "w1" in document["ui_meta"]["hermie-app"]["push"]["registrations"]

    # Retired: the next notification does not ask the push service again.
    assert module.deliver(approval(2)) == 0
    assert asked == ["https://push.example/w1"]

    # The device subscribed again and rewrote its row: live again.
    push_service(monkeypatch, status=201)
    rewrite_rows(home, {"w1": webpush_row(updatedAt=int(time.time()) + 60)})
    assert module.deliver(approval(3)) == 1


def test_a_403_for_a_row_on_this_gateways_key_is_said_and_not_retired(tmp_path, monkeypatch, caplog):
    """The key cannot be the problem, so the row must not go dark over it.

    Apple, for one, answers 403 `BadJwtToken` for a `sub` it will not take. A
    retired row only comes back when the device writes it again, and a client
    whose key already matches has no reason to.
    """
    home, module = loaded_push(tmp_path, monkeypatch)
    rewrite_rows(home, {"w1": webpush_row(applicationServerKey=module.web_push_public_key())})
    asked = push_service(monkeypatch, status=403, error='{"reason":"BadJwtToken"}')

    with caplog.at_level("WARNING"):
        assert module.deliver(approval(1)) == 0
        assert module.deliver(approval(2)) == 0

    assert asked == ["https://push.example/w1", "https://push.example/w1"]
    assert "w1" not in module.runtime.state.data["retired"]
    said = [record.getMessage() for record in caplog.records if "w1" in record.getMessage()]
    assert len(said) == 1
    assert "BadJwtToken" in said[0]


def test_a_403_that_says_the_key_is_wrong_retires_even_a_row_naming_this_key(tmp_path, monkeypatch):
    home, module = loaded_push(tmp_path, monkeypatch)
    rewrite_rows(home, {"w1": webpush_row(applicationServerKey=module.web_push_public_key())})
    push_service(monkeypatch, status=403, error=APPLE_MISMATCH)

    assert module.deliver(approval(1)) == 0
    assert module.runtime.state.data["retired"]["w1"]["reason"] == "webpush-key"


def test_a_probe_loads_the_key_but_never_mints_one(tmp_path, monkeypatch):
    """`hermes plugins validate` and `doctor` register against a context with no state."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    del ctx.state
    key_file = home / "plugin-data" / "hermie" / "vapid.pem"

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert not key_file.exists()
    assert "webPush" not in advert
    assert contract.CAP_PUSH_WEBPUSH_KEY not in contract.read_capabilities(advert)

    # A key the serving gateway already minted is read and published.
    minted = webpush.load_or_create_key(key_file)
    hermie_plugin.register(ctx)
    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert advert["webPush"] == {"publicKey": webpush.public_key_b64(minted)}


def test_an_unreadable_key_is_said_once_not_once_per_row(tmp_path, monkeypatch, caplog):
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(
            registrations={
                "w1": webpush_row(),
                "w2": webpush_row(endpoint="https://push.example/w2"),
            }
        ),
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    path = ctx.state.data_dir / "vapid.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a PEM file")
    asked = push_service(monkeypatch)

    with caplog.at_level("WARNING"):
        module = push_pkg.register(ctx, hermie_plugin.Runtime(ctx, home=home))
        assert module.deliver(approval(1)) == 0
        assert module.deliver(approval(2)) == 0

    assert asked == []
    assert len([record for record in caplog.records if "VAPID key" in record.getMessage()]) == 1
    assert path.read_text() == "this is not a PEM file"

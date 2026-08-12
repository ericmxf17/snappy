from types import SimpleNamespace

import access
import auth
import config
import snaptrade_client_wrapper as st
import ui


def test_personal_keys_prefer_keychain_over_environment(monkeypatch):
    monkeypatch.setattr(
        access, "_read",
        lambda account: ('{"client_id":"PERS-SAVED","consumer_key":"saved-secret"}'
                         if account == access.KEY_ACCOUNT else ""),
    )
    monkeypatch.setattr(config, "SNAPTRADE_CLIENT_ID", "PERS-ENV")
    monkeypatch.setattr(config, "SNAPTRADE_CONSUMER_KEY", "env-secret")

    assert access.personal_keys() == ("PERS-SAVED", "saved-secret")


def test_personal_key_client_uses_sdk_required_personal_placeholders(monkeypatch):
    made = {}

    class Client:
        def __init__(self, **kwargs):
            made.update(kwargs)

    monkeypatch.setattr(access, "personal_keys", lambda: ("PERS-ID", "secret"))
    monkeypatch.setattr(st, "SnapTrade", Client)

    st.connect(force="keys")

    assert made == {"client_id": "PERS-ID", "consumer_key": "secret"}
    assert st._USER == {
        "user_id": config.SNAPTRADE_USER_ID,
        "user_secret": config.SNAPTRADE_USER_SECRET,
    }


def test_user_can_choose_keys_while_oauth_token_remains_saved(monkeypatch):
    monkeypatch.setattr(config, "FORCE_AUTH_MODE", None)
    monkeypatch.setattr(access, "preferred_mode", lambda: "keys")
    monkeypatch.setattr(access, "personal_keys", lambda: ("PERS-ID", "secret"))
    monkeypatch.setattr(auth, "signed_in", lambda: True)

    assert st.mode() == "keys"


def test_user_can_switch_back_to_oauth_without_deleting_keys(monkeypatch):
    monkeypatch.setattr(config, "FORCE_AUTH_MODE", None)
    monkeypatch.setattr(access, "preferred_mode", lambda: "oauth")
    monkeypatch.setattr(access, "personal_keys", lambda: ("PERS-ID", "secret"))
    monkeypatch.setattr(auth, "signed_in", lambda: True)

    assert st.mode() == "oauth"


def test_permission_change_reauthorizes_the_exact_connection(monkeypatch):
    captured = {}

    def login(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(body={"redirectURI": "https://example.test/reauthorize"})

    monkeypatch.setattr(st, "mode", lambda: "keys")
    monkeypatch.setattr(
        st, "list_connections",
        lambda: [{"connection_id": "connection-2", "type": "read"}],
    )
    monkeypatch.setattr(
        st, "_client",
        SimpleNamespace(authentication=SimpleNamespace(login_snap_trade_user=login)),
    )
    monkeypatch.setattr(st, "_USER", {})

    url = st.connection_permission_url("connection-2", "trade")

    assert url == "https://example.test/reauthorize"
    assert captured["reconnect"] == "connection-2"
    assert captured["connection_type"] == "trade"


def test_drag_strip_accepts_first_click_and_explicitly_drags_window():
    assert "acceptsFirstMouse_" in vars(ui._DragStrip)
    assert "mouseDown_" in vars(ui._DragStrip)


def test_panel_exposes_read_and_full_permission_control():
    source = open(ui.PAGE, encoding="utf-8").read()
    assert 'id="accessMode"' in source
    assert "type: 'access'" in source
    assert "Full permission" in source
    assert "OAuth read-only" in source
    assert "Snappy access" in source


def test_first_full_permission_click_opens_snaptrade_dashboard(
    monkeypatch, state_reset
):
    import main

    opened = []
    monkeypatch.setattr(main.access, "saved_personal_keys", lambda: None)
    monkeypatch.setattr(main.webbrowser, "open", opened.append)

    main.Snappy.change_access(object(), "trade", "connection-7")

    assert opened == [main.SNAPTRADE_DASHBOARD]
    assert state_reset.STATE["key_setup_connection"] == "connection-7"
    assert "Continue setup" in state_reset.STATE["notice"]


def test_mismatched_personal_keys_are_rejected_and_forgotten(monkeypatch, state_reset):
    import main

    modes = []
    forgotten = []
    notices = []
    monkeypatch.setattr(main.access, "set_preferred_mode", modes.append)
    monkeypatch.setattr(main.access, "forget_personal_keys", lambda: forgotten.append(True))
    monkeypatch.setattr(main.st, "connect", lambda force=None: None)
    monkeypatch.setattr(main.st, "list_connections", lambda: [])
    monkeypatch.setattr(main.st, "mode", lambda: "oauth")
    monkeypatch.setattr(main.auth, "signed_in", lambda: True)
    monkeypatch.setattr(main, "notify", notices.append)

    main.Snappy._change_access(object(), "trade", "old-connection", "oauth")

    assert forgotten == [True]
    assert modes == ["keys", "oauth"]
    assert "don't match the signed-in SnapTrade account" in notices[-1]
    assert state_reset.STATE["permission_changing"] is None


def test_keychain_access_clear_removes_keys_and_mode(monkeypatch):
    deleted = []
    monkeypatch.setattr(access, "_delete", deleted.append)

    access.clear()

    assert deleted == [access.KEY_ACCOUNT, access.MODE_ACCOUNT]


def test_explicit_signout_clears_oauth_session(monkeypatch, state_reset):
    import main

    cleared = []
    monkeypatch.setattr(main.auth, "sign_out", lambda: cleared.append("oauth"))
    monkeypatch.setattr(main.st, "connect", lambda: cleared.append("reconnect"))
    monkeypatch.setattr(main.st, "mode", lambda: "none")
    monkeypatch.setattr(main, "notify", lambda message: cleared.append(message))

    main.Snappy.do_signout(object())

    assert cleared == ["oauth", "reconnect", "Signed out of SnapTrade."]
    assert state_reset.STATE["oauth_available"] is False

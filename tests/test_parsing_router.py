from james_monitoring.router import group_targets, is_status_request
from james_monitoring.runtime import parse_model_output


def test_parse_plain_json():
    r, a = parse_model_output('{"reply": "hi", "actions": [{"type": "remember", "note": "x"}]}')
    assert r == "hi" and a[0]["type"] == "remember"


def test_parse_fenced_and_prose():
    r, a = parse_model_output('Sure!\n```json\n{"reply": "ok", "actions": []}\n```')
    assert r == "ok" and a == []
    r, a = parse_model_output('Here you go: {"reply": "x", "actions": [{"note": "no type"}]} thanks')
    assert r == "x" and a == []


def test_parse_non_json_falls_back_to_text():
    r, a = parse_model_output("just words")
    assert r == "just words" and a == []


def test_group_routing(cfg):
    assert group_targets("@all give status", cfg) == (True, ["james", "sofia", "marcus"])
    assert group_targets("@Sofia change the button", cfg) == (False, ["sofia"])
    assert group_targets("@sofia_cd_bot hi", cfg, {"sofia": "sofia_cd_bot"}) == (False, ["sofia"])
    assert group_targets("@sofia @marcus sync up", cfg) == (False, ["sofia", "marcus"])
    assert group_targets("what is blocked?", cfg) == (False, ["james"])


def test_status_detection():
    assert is_status_request("@all give status")
    assert is_status_request("@all status?")
    assert not is_status_request("@all please rewrite the status page copy for the new pricing and add the FAQ section")
    assert not is_status_request("@all start working on Janus")

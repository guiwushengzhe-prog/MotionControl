from types import SimpleNamespace

import pytest

from motioncontrol.gunmouse import PORTAL_ORIGIN, request_origin_allowed
from test_configuration_coordination import application
from test_profile_api_v2 import request_for


def make_request(app, path, replies, origin=PORTAL_ORIGIN):
    request = request_for(app, path, replies)
    request.server = SimpleNamespace(server_port=8766)
    request.headers = {'Origin': origin}
    return request


def test_portal_reads_only_gunmouse_state_and_writes_shared_config(application, monkeypatch):
    app, replies = application, []
    monkeypatch.setattr(app.GUNMOUSE, 'start', lambda: None)
    request = make_request(app, '/api/gunmouse/status', replies)
    request.do_GET()
    assert replies[-1][0] == 200
    assert 'gunmouse' in replies[-1][1] and 'kernel' not in replies[-1][1]
    previous = replies[-1][1]['config_revision']
    request = make_request(app, '/api/gunmouse/config', replies)
    request._body = lambda: {'strength_mg': 1750}
    request.do_POST()
    assert replies[-1][0] == 200
    assert replies[-1][1]['gunmouse']['config']['strength_mg'] == 1750
    assert replies[-1][1]['config_revision'] > previous
    assert 'kernel' not in replies[-1][1]
    assert app.KERNEL.general_setting('gunmouse')['strength_mg'] == 1750
    assert not app.OUTPUT.enabled


@pytest.mark.parametrize('path', ['/api/gunmouse/status', '/api/gunmouse/config', '/api/gunmouse/devices'])
def test_unrelated_site_cannot_read_or_change_gunmouse(application, path):
    app, replies = application, []
    request = make_request(app, path, replies, 'https://unrelated.example')
    request._body = lambda: {'enabled': True}
    (request.do_POST if path.endswith('config') else request.do_GET)()
    assert replies[-1][0] == 403
    assert app.GUNMOUSE.status()['config']['enabled'] is False


def test_only_specific_portal_routes_allow_browser_preflight(application):
    app, replies = application, []
    request = make_request(app, '/api/gunmouse/config', replies)
    request.headers.update({'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
    status, headers = [], {}
    request.send_response = status.append
    request.send_header = lambda name,value: headers.update({name:value})
    request.end_headers = lambda: None
    request.do_OPTIONS()
    assert status == [204]
    assert headers['Access-Control-Allow-Methods'] == 'POST'
    request.path = '/api/output/config'
    request.do_OPTIONS()
    assert replies[-1][0] == 403


def test_local_page_and_native_calls_still_work():
    assert request_origin_allowed(None, 8766)
    assert request_origin_allowed('http://127.0.0.1:8766', 8766)
    assert not request_origin_allowed('null', 8766)
    assert not request_origin_allowed('https://guiwu-aware.icu.unrelated.example', 8766)

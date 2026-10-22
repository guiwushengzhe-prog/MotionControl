"""本机实验接口接入真实处理器，服务状态隔离，不启动网络或设备。"""
from test_configuration_coordination import application  # noqa: F401
from test_profile_api_v2 import request_for


def test_experiment_config_and_neutral_use_the_shared_runtime_revision(application):
    app, replies = application, []
    revision = app.CONFIG_REVISION
    request = request_for(app, "/api/sensitivity-experiments", replies)
    request._body = lambda: {"head_hold": True, "body_source": "depth"}
    request.do_POST()
    status, result = replies[-1]
    assert status == 200 and result["ok"]
    assert result["config_revision"] == revision + 1
    config = result["kernel"]["sensitivity_experiments"]["config"]
    assert config["head_hold"] and config["body_source"] == "depth"
    assert app.KERNEL.head_controller.hfhpe_hold
    request.path = "/api/sensitivity-experiments/neutral"
    request._body = lambda: {}
    request.do_POST()
    assert replies[-1][0] == 200
    assert replies[-1][1]["config_revision"] == revision + 2
    sources = replies[-1][1]["kernel"]["sensitivity_experiments"]["body"]["sources"]
    assert all(sources[name]["calibrating"] for name in ("model", "depth"))
    assert not sources["monocular"]["calibrating"]  # 单目自己的三秒基准由到来的深度区域建立。


def test_invalid_experiment_does_not_change_revision_and_remote_cannot_enable(application):
    app, replies = application, []
    revision = app.CONFIG_REVISION
    request = request_for(app, "/api/sensitivity-experiments", replies)
    request._body = lambda: {"body_source": "auto"}
    request.do_POST()
    assert replies[-1][0] == 400
    assert app.CONFIG_REVISION == revision
    request._is_loopback = lambda: False
    request._body = lambda: {"head_hold": True}
    request.do_POST()
    assert replies[-1][0] == 403
    assert app.CONFIG_REVISION == revision

import json

from playwright.sync_api import expect
from test_web_optimization import desktop, mock_browser


def open_diagnostics(page):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')


def test_diagnostic_export_downloads_the_summary_and_handles_failure(desktop, tmp_path):
    page = desktop.page
    requests = []
    def export(route):
        requests.append(route.request.post_data_json)
        route.fulfill(body=json.dumps({"schema": "motioncontrol.diagnostic-summary.v1"}),
            content_type="application/json", headers={"Content-Disposition": 'attachment; filename="summary.json"'})
    page.route('**/api/diagnostics/export', export)
    help_button = page.locator('#helpBtn')
    export_item = page.locator('#helpDiagnosticExportBtn')
    help_button.click()
    export_item.focus()
    with page.expect_download() as download:
        export_item.press('Enter')
    expect(page.locator('#helpMenu')).to_be_hidden()
    expect(help_button).to_be_focused()
    expect(page.locator('#notice')).to_contain_text('摘要已生成')
    target = tmp_path / "summary.json"
    download.value.save_as(target)
    assert json.loads(target.read_bytes())["schema"] == "motioncontrol.diagnostic-summary.v1"
    assert requests == [{}]
    open_diagnostics(page)
    page.route('**/api/diagnostics/export', lambda route: route.fulfill(status=400,
        content_type="application/json", body=json.dumps({"error": "诊断摘要未能生成，请稍后重试"})))
    page.click('#diagnosticExportBtn')
    page.wait_for_function("diagnosticExportStatus.textContent.includes('未能生成')")
    assert not page.locator('#diagnosticExportBtn').is_disabled()

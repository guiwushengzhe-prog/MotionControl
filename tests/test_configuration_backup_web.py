"""真实浏览器中的备份预览、取消、明确恢复及失败反馈。"""
from test_web_optimization import desktop, mock_browser


def open_backup(page):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')


def choose_backup(page):
    page.locator('#backupFile').set_input_files({"name": "MotionControl.zip", "mimeType": "application/zip", "buffer": b"isolated package"})
    page.wait_for_function('backupPreviewDialog.open && !backupRestoreBtn.disabled')


def test_backup_preview_collapses_details_and_cancel_never_changes_configuration(desktop):
    page, api = desktop.page, desktop.api
    open_backup(page)
    choose_backup(page)
    assert '6 个游戏配置' in page.locator('#backupPreviewSummary').inner_text()
    assert '1 个自建游戏' in page.locator('#backupPreviewSummary').inner_text()
    assert page.locator('#backupRestoreMode').input_value() == 'full'
    assert not page.locator('#backupPreviewDialog details').evaluate('el=>el.open')
    page.click('#backupPreviewDialog summary')
    assert 'game_profile_selection.json' in page.locator('#backupPreviewFiles').inner_text()
    page.click('#backupPreviewClose')
    assert not page.locator('#backupPreviewDialog').evaluate('el=>el.open')
    assert not any(path == '/api/data-backup/restore' for path, _ in api.requests)


def test_restore_uses_selected_mode_once_and_waits_for_real_exit_confirmation(desktop):
    page, api = desktop.page, desktop.api
    open_backup(page)
    choose_backup(page)
    page.select_option('#backupRestoreMode', 'portable')
    page.click('#backupRestoreBtn')
    page.wait_for_function("exitDialog.open && exitTitle.textContent === '备份已准备恢复'")
    requests = [body for path, body in api.requests if path == '/api/data-backup/restore']
    assert requests == [{"token": "a" * 64, "mode": "portable"}]
    assert '下次启动会自动恢复配置' in page.locator('#exitStatus').inner_text()
    assert page.locator('main').evaluate('el=>el.inert')


def test_corrupt_backup_stays_on_page_and_explains_failure(desktop):
    page, api = desktop.page, desktop.api
    open_backup(page)
    page.route('**/api/data-backup/preview', lambda route: api.fulfill(route, {"error": "备份内容不完整，原配置已保留"}, 400))
    page.locator('#backupFile').set_input_files({"name": "broken.zip", "mimeType": "application/zip", "buffer": b"broken"})
    page.wait_for_function("backupStatus.textContent.includes('不完整')")
    assert not page.locator('#backupPreviewDialog').evaluate('el=>el.open')
    assert not page.locator('#backupImportBtn').is_disabled()
    assert not page.locator('main').evaluate('el=>el.inert')


def test_export_download_reports_failure_without_claiming_saved(desktop):
    page, api = desktop.page, desktop.api
    open_backup(page)
    page.route('**/api/data-backup/export', lambda route: api.fulfill(route, {"error": "动作配置未能保存，请先重试"}, 400))
    page.click('#backupExportBtn')
    page.wait_for_function("backupStatus.textContent.includes('未能保存')")
    assert '备份已生成' not in page.locator('#backupStatus').inner_text()


def test_export_download_has_the_package_name_and_bytes(desktop, tmp_path):
    page = desktop.page
    open_backup(page)
    page.route('**/api/data-backup/export', lambda route: route.fulfill(status=200, content_type='application/zip',
        headers={'Content-Disposition': 'attachment; filename="MotionControl-20261026.zip"'}, body=b'package bytes'))
    with page.expect_download() as download:
        page.click('#backupExportBtn')
    downloaded = download.value
    assert downloaded.suggested_filename == 'MotionControl-20261026.zip'
    downloaded.save_as(str(tmp_path / downloaded.suggested_filename))
    assert (tmp_path / downloaded.suggested_filename).read_bytes() == b'package bytes'

from __future__ import annotations

from steam_seed_catalog import PRIORITY_GAMES, collect_top_seller_seeds, merge_seed_games, parse_search_html


HTML = '''
<a href="https://store.steampowered.com/app/10/Foo/" class="search_result_row ds_collapse_flag" data-ds-appid="10">
  <div><span class="title">Foo Game</span></div>
</a>
<a href="https://store.steampowered.com/app/20/Bar/" class="search_result_row" data-ds-appid="20">
  <span class="title">Bar &amp; Baz</span>
</a>
'''


def test_parse_search_html_gets_appid_and_name():
    rows = parse_search_html(HTML)
    assert rows == [{"appid": 10, "name": "Foo Game"}, {"appid": 20, "name": "Bar & Baz"}]


def test_merge_priority_first_and_deduplicates():
    priority = [{"appid": 2, "name": "Priority", "priority": True}]
    discovered = [{"appid": 1, "name": "One"}, {"appid": 2, "name": "Duplicate"}, {"appid": 3, "name": "Three"}]
    merged = merge_seed_games(priority, discovered, limit=3)
    assert [x["appid"] for x in merged] == [2, 1, 3]
    assert merged[0]["priority"] is True


class FakePages:
    def page(self, page):
        if page == 1:
            return [{"appid": 7000001, "name": "A"}, {"appid": 7000002, "name": "B"}]
        if page == 2:
            return [{"appid": 7000003, "name": "C"}]
        return []


def test_collection_target_is_report_only_no_failure_gate():
    games, report = collect_top_seller_seeds(FakePages(), count=len(PRIORITY_GAMES) + 10, max_pages=2)
    assert len(games) == len(PRIORITY_GAMES) + 3
    assert report["target_met"] is False
    assert report["build_gate"] is False

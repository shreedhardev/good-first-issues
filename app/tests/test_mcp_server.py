import asyncio
import sys
from unittest.mock import MagicMock, patch
from datetime import date, timedelta

import pytest
import requests

from mcp.server.mcpserver import MCPServer

from app import mcp_server
from app.core.config import DATASET_CACHE_TTL_SECONDS, PUBLISHED_DATASET_URL
from app.core.custom_exceptions import DatasetError


CSV_HEADER = 'repo,language,title,url,comments,labels,created_at,updated_at\n'

CSV_ROWS = (
    'owner/alpha,Python,Fix the parser,https://github.com/owner/alpha/issues/1,'
    '0,"[\'good first issue\']",2026-01-01,2026-01-02\n'
    'other/gamma,Go,Add a retry,https://github.com/other/gamma/issues/3,'
    '2,"[\'good first issue\']",2026-01-05,2026-01-06\n'
)


@pytest.fixture
def dataset_env(tmp_path, monkeypatch):
    csv_file = tmp_path / 'issues.csv'
    csv_file.write_text(CSV_HEADER + CSV_ROWS, encoding='utf-8')
    monkeypatch.setenv('ISSUES_CSV', str(csv_file))
    return str(csv_file)


@pytest.fixture(autouse=True)
def reset_published_cache():
    mcp_server._published_cache['issues'] = None
    mcp_server._published_cache['fetched_at'] = 0.0
    mcp_server._dataset_meta['source'] = None
    mcp_server._dataset_meta['location'] = None
    yield
    mcp_server._published_cache['issues'] = None
    mcp_server._published_cache['fetched_at'] = 0.0
    mcp_server._dataset_meta['source'] = None
    mcp_server._dataset_meta['location'] = None


def _published_response(text=None):
    response = MagicMock()
    response.text = text if text is not None else CSV_HEADER + CSV_ROWS
    response.raise_for_status.return_value = None
    return response


class TestLoadDataset:

    def test_load_dataset_reads_the_configured_file(self, dataset_env):
        result = mcp_server.load_dataset()

        assert len(result) == 2
        assert result[0]['repo'] == 'owner/alpha'
        assert 'dataset_source' not in result[0]

    def test_load_dataset_does_not_fetch_when_issues_csv_is_missing(
        self, tmp_path, monkeypatch
    ):
        missing = tmp_path / 'nope.csv'
        monkeypatch.setenv('ISSUES_CSV', str(missing))

        with patch('app.mcp_server.requests.get') as mock_get:
            with pytest.raises(DatasetError):
                mcp_server.load_dataset()

        mock_get.assert_not_called()

    def test_load_dataset_reads_the_local_file_when_env_is_unset(
        self, tmp_path, monkeypatch
    ):
        csv_file = tmp_path / 'good_first_issues.csv'
        csv_file.write_text(CSV_HEADER + CSV_ROWS, encoding='utf-8')
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(mcp_server, 'get_dataset_path', lambda: str(csv_file))

        with patch('app.mcp_server.requests.get') as mock_get:
            result = mcp_server.load_dataset()

        mock_get.assert_not_called()
        assert result[0]['repo'] == 'owner/alpha'

    def test_load_dataset_fetches_the_published_file_when_nothing_local(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path', lambda: str(tmp_path / 'missing.csv')
        )

        with patch(
            'app.mcp_server.requests.get', return_value=_published_response()
        ) as mock_get:
            result = mcp_server.load_dataset()

        mock_get.assert_called_once_with(
            PUBLISHED_DATASET_URL,
            timeout=mcp_server.DATASET_FETCH_TIMEOUT,
        )
        assert len(result) == 2
        assert result[0]['repo'] == 'owner/alpha'

    def test_load_dataset_reuses_the_published_cache(self, tmp_path, monkeypatch):
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path', lambda: str(tmp_path / 'missing.csv')
        )
        monkeypatch.setattr(mcp_server.time, 'monotonic', lambda: 1000.0)

        with patch(
            'app.mcp_server.requests.get', return_value=_published_response()
        ) as mock_get:
            first = mcp_server.load_dataset()
            second = mcp_server.load_dataset()

        assert mock_get.call_count == 1
        assert first == second

    def test_load_dataset_refetches_when_the_cache_expires(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path', lambda: str(tmp_path / 'missing.csv')
        )
        current = {'t': 1000.0}
        monkeypatch.setattr(mcp_server.time, 'monotonic', lambda: current['t'])

        with patch(
            'app.mcp_server.requests.get', return_value=_published_response()
        ) as mock_get:
            mcp_server.load_dataset()
            current['t'] = 1000.0 + DATASET_CACHE_TTL_SECONDS + 1
            mcp_server.load_dataset()

        assert mock_get.call_count == 2

    def test_load_dataset_uses_stale_cache_when_a_refetch_fails(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path', lambda: str(tmp_path / 'missing.csv')
        )
        current = {'t': 1000.0}
        monkeypatch.setattr(mcp_server.time, 'monotonic', lambda: current['t'])

        with patch(
            'app.mcp_server.requests.get',
            side_effect=[
                _published_response(),
                requests.exceptions.ConnectionError('offline'),
            ],
        ) as mock_get:
            first = mcp_server.load_dataset()
            current['t'] = 1000.0 + DATASET_CACHE_TTL_SECONDS + 1
            second = mcp_server.load_dataset()

        assert mock_get.call_count == 2
        assert second[0]['repo'] == first[0]['repo']

    def test_load_dataset_errors_when_the_published_fetch_fails(
        self, tmp_path, monkeypatch
    ):
        missing = tmp_path / 'missing.csv'
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(mcp_server, 'get_dataset_path', lambda: str(missing))

        with patch(
            'app.mcp_server.requests.get',
            side_effect=requests.exceptions.Timeout('timed out'),
        ):
            with pytest.raises(DatasetError) as excinfo:
                mcp_server.load_dataset()

        message = str(excinfo.value)
        assert str(missing) in message
        assert PUBLISHED_DATASET_URL in message
        assert 'timed out' in message

    def test_load_dataset_errors_on_an_http_failure(self, tmp_path, monkeypatch):
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path', lambda: str(tmp_path / 'missing.csv')
        )
        response = MagicMock()
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            '404'
        )

        with patch('app.mcp_server.requests.get', return_value=response):
            with pytest.raises(DatasetError) as excinfo:
                mcp_server.load_dataset()

        assert '404' in str(excinfo.value)


class TestSearchIssues:

    def test_search_issues_without_filters(self, dataset_env):
        result = mcp_server.search_issues()

        assert [i['comments'] for i in result] == [0, 2]

    def test_search_issues_with_filters(self, dataset_env):
        result = mcp_server.search_issues(
            language='Python',
            max_comments=0,
            label='good first issue',
            repo='owner',
            limit=5,
        )

        assert len(result) == 1
        assert result[0]['repo'] == 'owner/alpha'

    def test_search_issues_with_filter_max_age_days(self, tmp_path, monkeypatch):

        updated_at = date.today().isoformat()
        created_at = (date.today() - timedelta(days=200)).isoformat()
        csv_content = (
            CSV_HEADER +
            f'owner/recent,Python,Fresh issue,https://github.com/owner/recent/issues/1,'
            f'0,"[\'good first issue\']",{updated_at},{updated_at}\n'
            f'owner/old,Python,Stale issue,https://github.com/owner/old/issues/2,'
            f'0,"[\'good first issue\']",{created_at},{created_at}\n'
        )
        csv_file = tmp_path / 'issues.csv'
        csv_file.write_text(csv_content, encoding='utf-8')
        monkeypatch.setenv('ISSUES_CSV', str(csv_file)) 
        result = mcp_server.search_issues(
            max_age_days=0
        )

        assert len(result) == 1
        assert result[0]['repo'] == 'owner/recent'

        


class TestListLanguages:

    def test_list_languages_counts_every_language(self, dataset_env):
        result = mcp_server.list_languages()

        assert result == [
            {'language': 'Go', 'issues': 1},
            {'language': 'Python', 'issues': 1},
        ]


class TestListRepositories:

    def test_list_repositories_lists_them_all(self, dataset_env):
        result = mcp_server.list_repositories()

        assert len(result) == 2

    def test_list_repositories_narrowed_to_a_language(self, dataset_env):
        result = mcp_server.list_repositories(language='Go')

        assert result == [{'repo': 'other/gamma', 'issues': 1}]


class TestToolRegistration:

    def test_every_tool_is_registered(self):
        tools = asyncio.run(mcp_server.mcp.list_tools())

        names = {tool.name for tool in tools}
        assert names == {
            'search_issues', 'list_languages', 'list_repositories',
            'dataset_info',
        }

    def test_every_tool_is_described_for_the_model(self):
        tools = asyncio.run(mcp_server.mcp.list_tools())

        assert all(tool.description for tool in tools)

    def test_search_issues_exposes_its_filters(self):
        tools = asyncio.run(mcp_server.mcp.list_tools())

        search = next(t for t in tools if t.name == 'search_issues')
        assert set(search.input_schema['properties']) == {
            'language', 'max_comments', 'label', 'repo', 'limit', 'max_age_days',
        }


class TestDatasetInfo:

    def test_dataset_info_reports_issues_csv(self, dataset_env):
        info = mcp_server.dataset_info()

        assert info['source'] == 'ISSUES_CSV'
        assert info['location'] == dataset_env

    def test_dataset_info_reports_local(self, tmp_path, monkeypatch):
        csv_file = tmp_path / 'good_first_issues.csv'
        csv_file.write_text(CSV_HEADER + CSV_ROWS, encoding='utf-8')
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path', lambda: str(csv_file)
        )

        info = mcp_server.dataset_info()

        assert info['source'] == 'local'
        assert info['location'] == str(csv_file)

    def test_dataset_info_reports_published(self, tmp_path, monkeypatch):
        monkeypatch.delenv('ISSUES_CSV', raising=False)
        monkeypatch.setattr(
            mcp_server, 'get_dataset_path',
            lambda: str(tmp_path / 'missing.csv'),
        )

        with patch(
            'app.mcp_server.requests.get',
            return_value=_published_response(),
        ):
            info = mcp_server.dataset_info()

        assert info['source'] == 'published'
        assert info['location'] == PUBLISHED_DATASET_URL


def test_mcp_server_main_block(monkeypatch):
    """Test the __main__ block of mcp_server.py."""
    monkeypatch.setattr(sys, 'argv', ['mcp_server.py'])

    with patch.object(MCPServer, 'run') as mock_run:
        sys.modules.pop('app.mcp_server', None)
        import runpy
        runpy.run_module('app.mcp_server', run_name='__main__', alter_sys=True)

    mock_run.assert_called_once()

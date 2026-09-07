import logging
import os
import time

import requests
from mcp.server.mcpserver import MCPServer

from app.core.config import (
    DATASET_CACHE_TTL_SECONDS,
    DATASET_FETCH_TIMEOUT,
    PUBLISHED_DATASET_URL,
    get_dataset_path,
)
from app.core.custom_exceptions import DatasetError
from app.core.dataset import DatasetManager


mcp = MCPServer(
    name='good-first-issues',
    instructions=(
        'Search the good first issues dataset this repository builds. '
        'The data is ISSUES_CSV if that is set, otherwise the local CSV '
        'if it is present, otherwise the published dataset on GitHub. '
        'Call dataset_info to see which of those answered. Call '
        'list_languages before search_issues to see which languages are '
        'actually present.'
    ),
)

_published_cache = {
    'issues': None,
    'fetched_at': 0.0,
}

_dataset_meta = {
    'source': None,
    'location': None,
}


def _remember_source(source, location):
    _dataset_meta['source'] = source
    _dataset_meta['location'] = location


def _fetch_published_dataset():
    response = requests.get(
        PUBLISHED_DATASET_URL,
        timeout=DATASET_FETCH_TIMEOUT,
    )
    response.raise_for_status()
    return DatasetManager.load_issues_from_text(response.text)


def load_dataset():
    """
    Returns the issues to search.

    Resolution order: ISSUES_CSV if set, then the local file if it is
    there, then the published CSV. The published copy is cached so
    tool calls do not fetch on every request. The source in use is
    stored for dataset_info.
    """
    env_path = os.environ.get('ISSUES_CSV')
    if env_path:
        logging.info('Loading issues dataset from ISSUES_CSV (%s)', env_path)
        issues = DatasetManager.load_issues(env_path)
        _remember_source('ISSUES_CSV', env_path)
        return issues

    local_path = get_dataset_path()
    if os.path.isfile(local_path):
        logging.info('Loading issues dataset from local file (%s)', local_path)
        issues = DatasetManager.load_issues(local_path)
        _remember_source('local', local_path)
        return issues

    now = time.monotonic()
    cached = _published_cache['issues']
    fetched_at = _published_cache['fetched_at']
    cache_is_fresh = (
        cached is not None
        and (now - fetched_at) < DATASET_CACHE_TTL_SECONDS
    )
    if cache_is_fresh:
        logging.info(
            'Loading issues dataset from published cache (%s)',
            PUBLISHED_DATASET_URL,
        )
        _remember_source('published', PUBLISHED_DATASET_URL)
        return cached

    try:
        issues = _fetch_published_dataset()
    except requests.RequestException as error:
        if cached is not None:
            logging.warning(
                'Fetch of published dataset failed (%s); using stale cache',
                error,
            )
            _remember_source('published', PUBLISHED_DATASET_URL)
            return cached
        raise DatasetError(
            PUBLISHED_DATASET_URL,
            reason=(
                f"no local dataset at {local_path}, and fetching the "
                f"published dataset from {PUBLISHED_DATASET_URL} failed: "
                f"{error}"
            ),
        ) from error

    _published_cache['issues'] = issues
    _published_cache['fetched_at'] = now
    logging.info(
        'Loading issues dataset from published file (%s)',
        PUBLISHED_DATASET_URL,
    )
    _remember_source('published', PUBLISHED_DATASET_URL)
    return issues


@mcp.tool()
def search_issues(
    language: str | None = None,
    max_comments: int | None = None,
    label: str | None = None,
    repo: str | None = None,
    limit: int = 20,
    max_age_days: int | None = None
) -> list[dict]:
    """
    Search the good first issues dataset, least discussed issues first.

    Set max_comments to 0 for issues nobody has commented on yet, which are
    the ones least likely to be claimed already. The language must match one
    reported by list_languages. The repo filter matches on a substring of
    owner/name, so "django" finds every Django repository at once.

    Set max_age_days to only return issues updated within that many days.
    An issue labeled good first issue that nobody has touched in years is
    not really available, and this dataset has plenty of those — use this
    filter when the goal is a currently active issue, not just any match.
    """
    return DatasetManager.filter_issues(
        load_dataset(),
        language=language,
        max_comments=max_comments,
        label=label,
        repo=repo,
        limit=limit,
        max_age_days=max_age_days
    )


@mcp.tool()
def list_languages() -> list[dict]:
    """
    List the languages present in the dataset with their issue count,
    most issues first. Use it to pick a valid language for search_issues.
    """
    return DatasetManager.count_by_language(load_dataset())


@mcp.tool()
def list_repositories(language: str | None = None) -> list[dict]:
    """
    List the repositories present in the dataset with their issue count,
    most issues first, optionally narrowed to a single language.
    """
    return DatasetManager.count_by_repo(load_dataset(), language=language)


@mcp.tool()
def dataset_info() -> dict:
    """
    Which dataset the other tools are reading.

    source is ISSUES_CSV, local, or published. location is the file path
    or the published URL.
    """
    load_dataset()
    return {
        'source': _dataset_meta['source'],
        'location': _dataset_meta['location'],
    }


if __name__ == '__main__':

    mcp.run()

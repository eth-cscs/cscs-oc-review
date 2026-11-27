"""Exercise complete REST traversal and rejection of malformed or failed pages."""

from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from common.github import rest_pages


@pytest.mark.parametrize("items_key", [None, "workflow_runs"])
@pytest.mark.parametrize("tail", [[], [{"id": 101}, {"id": 102}]])
def test_all_items_survive_page_boundaries_and_existing_query(items_key, tail):
    """Collect every page without losing query parameters or terminal-page records."""
    first = [{"id": index} for index in range(1, 101)]
    pages = [first, tail]
    calls = []

    def api(path):
        """Return the requested fixture page in the selected endpoint envelope."""
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        calls.append((parsed.path, query))
        records = pages[int(query["page"][0]) - 1]
        return records if items_key is None else {items_key: records}

    items = [item for page in rest_pages(api, "/items", params={"branch": "feature/a b"},
                                        items_key=items_key) for item in page]
    assert [item["id"] for item in items] == list(range(1, 101 + len(tail)))
    assert calls == [
        ("/items", {"branch": ["feature/a b"], "per_page": ["100"], "page": [str(page)]})
        for page in (1, 2)
    ]


@pytest.mark.parametrize("response,items_key", [
    ({"message": "failed"}, None),
    (None, None),
    ("not a list", None),
    ([{}] * 101, None),
    ([], "workflow_runs"),
    ({}, "workflow_runs"),
    ({"workflow_runs": {}}, "workflow_runs"),
    ({"workflow_runs": [{}] * 101}, "workflow_runs"),
])
def test_malformed_pages_cannot_look_like_complete_results(response, items_key):
    """Reject bad page shapes rather than reporting partial history as complete."""
    def api(path):
        """Return the selected malformed fixture payload without a network request."""
        return response

    with pytest.raises(ValueError):
        list(rest_pages(api, "/items", items_key=items_key))


def test_later_http_failure_is_not_silently_treated_as_end_of_results():
    """Propagate a later HTTP failure instead of accepting the pages already read."""
    failure = HTTPError("https://api.github.com/items", 403, "forbidden", {}, None)

    def api(path):
        """Return one full page before simulating a denied continuation request."""
        if parse_qs(urlsplit(path).query)["page"] == ["1"]:
            return [{"id": index} for index in range(100)]
        raise failure

    with pytest.raises(HTTPError) as caught:
        list(rest_pages(api, "/items"))
    assert caught.value is failure

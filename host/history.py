"""Fetch complete, inert pull request review history through the trusted API client."""

from common.github import rest_pages


COMMENT_FIELDS = """
  id
  author { login __typename }
  body createdAt updatedAt url
  commit { oid }
  originalCommit { oid }
  replyTo { id }
  pullRequestReview { fullDatabaseId }
  diffHunk path line originalLine
"""

THREAD_QUERY = """
query ReviewHistory($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id path line originalLine isOutdated isResolved
          resolvedBy { login }
          comments(first: 100) {
            nodes { %s }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""" % COMMENT_FIELDS

COMMENT_QUERY = """
query ReviewThreadComments($id: ID!, $cursor: String!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      id
      comments(first: 100, after: $cursor) {
        nodes { %s }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""" % COMMENT_FIELDS


def _record(value, fields):
    """Require a history object containing every field consumed by the caller."""
    if not isinstance(value, dict) or any(field not in value for field in fields):
        raise ValueError("malformed GitHub history object")
    return value


def _nullable_field(value, field):
    """Read an optional related object's field without inventing missing attribution."""
    return None if value is None else _record(value, (field,))[field]


def _author(value, type_field):
    """Preserve author identity and type, including deleted accounts."""
    if value is None:
        return None, None
    value = _record(value, ("login", type_field))
    return value["login"], value[type_field]


def _unique(records, seen):
    """Reject invalid or repeated identities across history pages."""
    for record in records:
        identity = _record(record, ("id",))["id"]
        if (type(identity) not in (int, str) or identity == "" or identity in seen):
            raise ValueError("invalid or repeated GitHub history id")
        seen.add(identity)


def _rest_items(path, api):
    """Yield complete REST history while rejecting overlapping pages."""
    seen = set()
    for items in rest_pages(api, path):
        # Repeated identities expose stalled or overlapping pages, not complete history.
        _unique(items, seen)
        yield from items


def _graphql(api, query, variables):
    """Require usable GraphQL data without accepting partial-error responses."""
    response = _record(api("/graphql", {"query": query, "variables": variables}), ("data",))
    if "errors" in response:
        raise ValueError("GitHub GraphQL returned history errors")
    return _record(response["data"], ())


def _connection(value, cursors, identities):
    """Validate a connection page and require unique nodes and advancing cursors."""
    value = _record(value, ("nodes", "pageInfo"))
    nodes = value["nodes"]
    info = _record(value["pageInfo"], ("hasNextPage", "endCursor"))
    more, cursor = info["hasNextPage"], info["endCursor"]
    # Require pagination to progress; partial history must not look like a complete review.
    if (not isinstance(nodes, list) or len(nodes) > 100 or type(more) is not bool
            or (cursor is not None and (not isinstance(cursor, str) or not cursor))
            or (nodes and cursor is None) or (more and not nodes)
            or (cursor is not None and cursor in cursors)):
        raise ValueError("malformed GitHub history cursor pagination")
    if cursor is not None:
        cursors.add(cursor)
    _unique(nodes, identities)
    return nodes, cursor if more else None


def _review(value):
    """Normalize a review while retaining attribution, state, and revision."""
    value = _record(value, ("id", "user", "body", "state", "commit_id", "html_url"))
    author, author_type = _author(value["user"], "type")
    return {
        "id": value["id"], "author": author, "author_type": author_type,
        "body": value["body"], "state": value["state"],
        "submitted_at": value.get("submitted_at"), "commit_id": value["commit_id"],
        "url": value["html_url"],
    }


def _conversation_comment(value):
    """Normalize a conversation comment without interpreting its body."""
    value = _record(value, ("id", "user", "body", "created_at", "updated_at", "html_url"))
    author, author_type = _author(value["user"], "type")
    return {
        "id": value["id"], "author": author, "author_type": author_type,
        "body": value["body"], "created_at": value["created_at"],
        "updated_at": value["updated_at"], "url": value["html_url"],
    }


def _thread_comment(value):
    """Normalize inline attribution, revision links, reply chains, and diff context."""
    value = _record(value, ("id", "author", "body", "createdAt", "updatedAt", "url",
                            "commit", "originalCommit", "replyTo", "pullRequestReview",
                            "diffHunk", "path", "line", "originalLine"))
    author, author_type = _author(value["author"], "__typename")
    review_id = _nullable_field(value["pullRequestReview"], "fullDatabaseId")
    return {
        "id": value["id"], "review_id": None if review_id is None else int(review_id),
        "author": author, "author_type": author_type, "body": value["body"],
        "created_at": value["createdAt"], "updated_at": value["updatedAt"], "url": value["url"],
        "commit_id": _nullable_field(value["commit"], "oid"),
        "original_commit_id": _nullable_field(value["originalCommit"], "oid"),
        "reply_to_id": _nullable_field(value["replyTo"], "id"),
        "diff_hunk": value["diffHunk"], "path": value["path"],
        "line": value["line"], "original_line": value["originalLine"],
    }


def _thread(value, api):
    """Collect every comment page for one identity-checked review thread."""
    value = _record(value, ("id", "path", "line", "originalLine", "isOutdated", "isResolved",
                            "resolvedBy", "comments"))
    result = {
        "id": value["id"], "path": value["path"], "line": value["line"],
        "original_line": value["originalLine"], "is_outdated": value["isOutdated"],
        "is_resolved": value["isResolved"], "resolved_by": _nullable_field(value["resolvedBy"], "login"),
        "comments": [],
    }
    connection = value["comments"]
    cursors, identities = set(), set()
    while True:
        comments, cursor = _connection(connection, cursors, identities)
        result["comments"].extend(_thread_comment(comment) for comment in comments)
        if cursor is None:
            return result
        data = _graphql(api, COMMENT_QUERY, {"id": value["id"], "cursor": cursor})
        node = _record(data.get("node"), ("id", "comments"))
        if node["id"] != value["id"]:
            raise ValueError("GitHub history thread identity mismatch")
        connection = node["comments"]


def fetch_history(repo, pr_number, api):
    """Fetch all reviews, conversation comments and complete inline discussions.

    The caller must authorize and validate the repository and PR before calling.
    API failures and incomplete/malformed pages fail preparation, never omit history.
    """
    result = {
        "reviews": [_review(value) for value in _rest_items(f"/repos/{repo}/pulls/{pr_number}/reviews", api)],
        "conversation_comments": [_conversation_comment(value) for value in
                                  _rest_items(f"/repos/{repo}/issues/{pr_number}/comments", api)],
        "review_threads": [],
    }
    owner, name = repo.split("/")
    cursor = None
    cursors, identities = set(), set()
    while True:
        data = _graphql(api, THREAD_QUERY, {"owner": owner, "name": name,
                                           "number": pr_number, "cursor": cursor})
        repository = _record(data.get("repository"), ("pullRequest",))
        pull_request = _record(repository["pullRequest"], ("reviewThreads",))
        threads, cursor = _connection(pull_request["reviewThreads"], cursors, identities)
        result["review_threads"].extend(_thread(thread, api) for thread in threads)
        if cursor is None:
            return result

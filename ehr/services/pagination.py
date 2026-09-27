"""Shared list-screen pagination (spec §36.5 item 13 / §18.3 item 5 follow-up):
"no pagination, advanced search, filters, or large-data handling on any list
screen." A plain offset/limit page-number scheme -- no cursor pagination or
infinite scroll -- matching the simplicity of every other UI convention in
this app; fine at the scale this app targets (a single practice), not
designed for very large datasets.
"""
from urllib.parse import urlencode

DEFAULT_PAGE_SIZE = 25


def paginate(query, page: int, page_size: int = DEFAULT_PAGE_SIZE):
    """Applies offset/limit to an already-filtered SQLAlchemy query. Returns
    (items, total, total_pages, page) -- `page` is clamped into
    [1, total_pages] so an out-of-range page number (a stale bookmark, a
    manually-edited URL) never 500s or silently returns nothing when there is
    real data on an earlier page."""
    page = max(1, page)
    total = query.count()
    total_pages = max(1, -(-total // page_size))  # ceiling division
    page = min(page, total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return items, total, total_pages, page


def pagination_context(request, page: int, total: int, total_pages: int, page_size: int = DEFAULT_PAGE_SIZE):
    """Builds the {page, total, total_pages, page_size, prev_url, next_url}
    dict the `_pagination.html` partial expects, preserving every other
    current query-string parameter (filters, sort, etc.) except `page`
    itself, so paging forward/back never drops an active filter."""
    params = dict(request.query_params)

    def url_for_page(p):
        params["page"] = str(p)
        return "?" + urlencode(params)

    return {
        "page": page, "total": total, "total_pages": total_pages, "page_size": page_size,
        "prev_url": url_for_page(page - 1) if page > 1 else None,
        "next_url": url_for_page(page + 1) if page < total_pages else None,
    }

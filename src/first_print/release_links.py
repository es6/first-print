"""Identity of release links used for alert merging and durable recovery."""

import re

_FILING_FOLDER = re.compile(r"https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/")
_PRN_EDITION = re.compile(r"https://www\.prnewswire\.com/news-releases/([^/?#]+)-\d{9}\.html")
# A link line's URL, in `[label](<url>)` or the older `label: <url>` form.
_LINE_URL = re.compile(r"<(https?://[^>\s]+)>")


def _line_url(line: str) -> str:
    urls = _LINE_URL.findall(line)
    return urls[-1] if urls else line


def _filing_folder(url: str) -> str | None:
    """The SEC filing folder a link points into, or None for any other link."""
    match = _FILING_FOLDER.match(url)
    return match.group(0) if match else None


def _same_release_link(a: str, b: str) -> bool:
    """Collapse alternate links to the same SEC filing or PR Newswire headline."""
    folder = _filing_folder(a)
    if folder is not None:
        return folder == _filing_folder(b)
    first, second = _PRN_EDITION.fullmatch(a), _PRN_EDITION.fullmatch(b)
    return bool(first and second and first[1] == second[1])

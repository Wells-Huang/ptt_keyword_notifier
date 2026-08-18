"""Cross-platform PTT Gamesale keyword monitor for GitHub Actions.

The module intentionally uses only Python's standard library so the same code
can run locally and on an Ubuntu GitHub-hosted runner.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PTT_ORIGIN = "https://www.ptt.cc"
DEFAULT_BOARD = "Gamesale"
SCHEMA_VERSION = 1
MAX_NOTIFIED = 1000
MAX_PENDING = 100


class NotifierError(Exception):
    """Base class for expected monitor errors."""


class ConfigError(NotifierError):
    """Configuration or state validation error."""


class PttFetchError(NotifierError):
    """PTT could not be fetched or parsed."""


class DiscordError(NotifierError):
    """Discord notification failed."""


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    url: str


GAME_PATTERN = re.compile(
    r"(?:真\s*)?三\s*[國国]\s*[無无]\s*[雙双].{0,40}?起\s*源"
    r"|Dynasty\s+Warriors.{0,30}?Origins",
    re.IGNORECASE,
)
PLATFORM_PATTERN = re.compile(
    r"(?:\[\s*NS\s*2\s*\]|Nintendo\s*Switch\s*2|Switch\s*2|\bNS\s*2\b)",
    re.IGNORECASE,
)
PREVIOUS_PAGE_PATTERN = re.compile(
    r'<a\s+[^>]*href=["\'](?P<href>/bbs/Gamesale/index\d+\.html)["\'][^>]*>'
    r"(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
ARTICLE_HREF_PATTERN = re.compile(r"^/bbs/Gamesale/M\.[^/]+\.html$")


class GamesaleParser(HTMLParser):
    """Extract article anchors from PTT's r-ent list items."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._entry_depth: int | None = None
        self._anchor_depth: int | None = None
        self._current_href: str | None = None
        self._title_parts: list[str] = []
        self.articles: list[Article] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if self._entry_depth is None:
            classes = (attrs_dict.get("class") or "").split()
            if tag.lower() == "div" and "r-ent" in classes:
                self._entry_depth = 1
            return

        self._entry_depth += 1
        if tag.lower() == "a" and self._current_href is None:
            href = attrs_dict.get("href") or ""
            if ARTICLE_HREF_PATTERN.fullmatch(href):
                self._current_href = href
                self._title_parts = []
                self._anchor_depth = self._entry_depth

    def handle_data(self, data: str) -> None:
        if self._anchor_depth is not None and self._current_href is not None:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._entry_depth is None:
            return

        if tag.lower() == "a" and self._anchor_depth == self._entry_depth:
            self._anchor_depth = None

        self._entry_depth -= 1
        if self._entry_depth == 0:
            if self._current_href is not None:
                title = normalize_title("".join(self._title_parts))
                if title:
                    self.articles.append(
                        Article(
                            id=self._current_href,
                            title=title,
                            url=urljoin(PTT_ORIGIN, self._current_href),
                        )
                    )
            self._entry_depth = None
            self._anchor_depth = None
            self._current_href = None
            self._title_parts = []


def normalize_title(value: str) -> str:
    value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_articles(document: str) -> list[Article]:
    parser = GamesaleParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:
        raise PttFetchError(f"PTT HTML parsing failed: {exc}") from exc
    return parser.articles


def get_previous_page_url(document: str) -> str | None:
    for match in PREVIOUS_PAGE_PATTERN.finditer(document):
        if "上頁" in html.unescape(match.group("label")):
            return urljoin(PTT_ORIGIN, match.group("href"))
    return None


def matches_target(title: str) -> bool:
    normalized = normalize_title(title)
    return bool(
        GAME_PATTERN.search(normalized)
        and PLATFORM_PATTERN.search(normalized)
        and "售" in normalized
    )


def validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a JSON object")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError("Unsupported configuration schema_version")
    if config.get("board") != DEFAULT_BOARD:
        raise ConfigError("Only the Gamesale board is supported")
    if config.get("pages") != 2:
        raise ConfigError("The monitor must check exactly two pages")
    try:
        ZoneInfo(str(config["timezone"]))
    except (KeyError, ZoneInfoNotFoundError) as exc:
        raise ConfigError("Configuration timezone must be a valid IANA timezone") from exc
    quiet = config.get("quiet_hours")
    if (
        not isinstance(quiet, dict)
        or not isinstance(quiet.get("start"), str)
        or not isinstance(quiet.get("end"), str)
    ):
        raise ConfigError("quiet_hours.start and quiet_hours.end are required")
    try:
        parse_clock(quiet["start"])
        parse_clock(quiet["end"])
    except ValueError as exc:
        raise ConfigError(f"Invalid quiet hour: {exc}") from exc
    request = config.get("request")
    if not isinstance(request, dict) or not isinstance(request.get("timeout_seconds"), int):
        raise ConfigError("request.timeout_seconds is required")
    if not isinstance(request.get("max_attempts"), int) or request["max_attempts"] < 1:
        raise ConfigError("request.max_attempts must be a positive integer")
    rules = config.get("rules")
    if not isinstance(rules, list) or not rules or not all(isinstance(rule, dict) for rule in rules):
        raise ConfigError("At least one rule is required")
    return config


def parse_clock(value: str) -> clock_time:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.time()


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Required file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc


def validate_state(state: object) -> dict:
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError("Unsupported or missing state schema_version")
    notified = state.get("notified")
    pending = state.get("pending")
    if not isinstance(notified, list) or not all(isinstance(item, str) for item in notified):
        raise ConfigError("state.notified must be a list of strings")
    if len(notified) > MAX_NOTIFIED:
        raise ConfigError("state.notified exceeds the supported limit")
    for item in notified:
        if not item.startswith("/bbs/Gamesale/"):
            raise ConfigError("state.notified contains an ID outside Gamesale")
    if not isinstance(pending, list) or len(pending) > MAX_PENDING:
        raise ConfigError("state.pending must be a list within the supported limit")
    seen_pending: set[str] = set()
    for item in pending:
        if not isinstance(item, dict):
            raise ConfigError("Each pending item must be an object")
        required = {"id", "title", "url", "found_at"}
        if set(item) != required or not all(isinstance(item[key], str) for key in required):
            raise ConfigError("Each pending item must contain exactly id, title, url, found_at strings")
        if (
            not item["id"].startswith("/bbs/Gamesale/")
            or not item["url"].startswith(PTT_ORIGIN + "/bbs/Gamesale/")
        ):
            raise ConfigError("Pending item URL is outside the fixed PTT Gamesale board")
        if item["id"] in seen_pending:
            raise ConfigError("Duplicate pending ID")
        seen_pending.add(item["id"])
    if len(set(notified)) != len(notified):
        raise ConfigError("Duplicate notified ID")
    return {
        "schema_version": SCHEMA_VERSION,
        "notified": list(notified),
        "pending": list(pending),
    }


def load_state(path: Path) -> dict:
    return validate_state(load_json(path))


def write_state_atomic(path: Path, state: dict) -> None:
    validated = validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def local_now(config: dict, override: str | None = None) -> datetime:
    zone = ZoneInfo(config["timezone"])
    if override is None:
        return datetime.now(timezone.utc).astimezone(zone)
    parsed = datetime.fromisoformat(override.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ConfigError("--now must include a timezone offset")
    return parsed.astimezone(zone)


def is_quiet_hours(now: datetime, config: dict) -> bool:
    current = now.timetz().replace(tzinfo=None)
    start = parse_clock(config["quiet_hours"]["start"])
    end = parse_clock(config["quiet_hours"]["end"])
    if start < end:
        return start <= current < end
    return current >= start or current < end


def fetch_page(url: str, config: dict) -> str:
    if not url.startswith(PTT_ORIGIN + "/"):
        raise PttFetchError("Refusing to fetch a URL outside ptt.cc")
    request_config = config["request"]
    request = Request(
        url,
        headers={
            "Cookie": "over18=1",
            "User-Agent": request_config["user_agent"],
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    attempts = request_config["max_attempts"]
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=request_config["timeout_seconds"]) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise PttFetchError(f"PTT request failed after {attempts} attempts: {last_error}")


def fetch_target_articles(config: dict) -> list[Article]:
    first_url = f"{PTT_ORIGIN}/bbs/{config['board']}/index.html"
    first_html = fetch_page(first_url, config)
    documents = [first_html]
    previous = get_previous_page_url(first_html)
    if config["pages"] >= 2 and previous:
        documents.append(fetch_page(previous, config))

    unique: dict[str, Article] = {}
    for document in documents:
        for article in parse_articles(document):
            if matches_target(article.title):
                unique.setdefault(article.id, article)
    return list(unique.values())


def send_discord(webhook_url: str, content: str) -> None:
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        raise DiscordError("DISCORD_WEBHOOK_URL is missing or has an unexpected host")
    payload = json.dumps(
        {
            "content": content[:1900],
            "allowed_mentions": {"parse": []},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "ptt-keyword-notifier/1.0",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise DiscordError(f"Discord returned HTTP {response.status}")
    except HTTPError as exc:
        raise DiscordError(f"Discord returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DiscordError(f"Discord request failed: {exc}") from exc


def pending_from_article(article: Article, now: datetime) -> dict[str, str]:
    return {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "found_at": now.isoformat(),
    }


def process_matches(
    state: dict,
    matches: Iterable[Article],
    now: datetime,
    config: dict,
    mode: str,
    notifier: Callable[[dict[str, str]], None],
) -> tuple[dict, list[dict[str, str]], list[dict[str, str]]]:
    """Return (new_state, failed_items, successful_notifications)."""
    working = copy.deepcopy(validate_state(state))
    notified = list(working["notified"])
    pending_by_id = {item["id"]: item for item in working["pending"]}
    for article in matches:
        if article.id not in notified and article.id not in pending_by_id:
            pending_by_id[article.id] = pending_from_article(article, now)

    if mode == "dry-run":
        return working, list(pending_by_id.values()), []
    if is_quiet_hours(now, config):
        working["pending"] = list(pending_by_id.values())[:MAX_PENDING]
        return working, working["pending"], []

    failures: list[dict[str, str]] = []
    successes: list[dict[str, str]] = []
    for item in list(pending_by_id.values()):
        try:
            notifier(item)
        except DiscordError:
            failures.append(item)
            continue
        if item["id"] not in notified:
            notified.insert(0, item["id"])
        pending_by_id.pop(item["id"], None)
        successes.append(item)

    working["notified"] = notified[:MAX_NOTIFIED]
    working["pending"] = list(pending_by_id.values())[:MAX_PENDING]
    return working, failures, successes


def ensure_secret(mode: str, webhook_url: str | None) -> str:
    if mode in {"normal", "test-discord"} and not webhook_url:
        raise ConfigError("DISCORD_WEBHOOK_URL is required for this mode")
    return webhook_url or ""


def run(config_path: Path, state_path: Path, mode: str, now_override: str | None) -> int:
    config = validate_config(load_json(config_path))
    webhook_url = ensure_secret(mode, os.environ.get("DISCORD_WEBHOOK_URL"))
    if mode == "normal" and now_override is not None:
        raise ConfigError("--now is not allowed in normal mode")

    if mode == "test-discord":
        send_discord(webhook_url, "PTT Gamesale monitor Discord test (GitHub Actions)")
        print("Discord test notification sent")
        return 0

    state = load_state(state_path)
    now = local_now(config, now_override)
    matches = fetch_target_articles(config)

    def notifier(item: dict[str, str]) -> None:
        send_discord(
            webhook_url,
            f"**PTT Gamesale target found**\n{item['title']}\n{item['url']}",
        )

    new_state, failures, successes = process_matches(state, matches, now, config, mode, notifier)
    print(f"Checked Gamesale: {len(matches)} matching listing(s); quiet={is_quiet_hours(now, config)}")
    for article in matches:
        print(f"MATCH: {article.title} | {article.url}")
    if mode == "dry-run":
        print("Dry-run: no Discord message sent and state was not written")
        return 0

    if new_state != state:
        write_state_atomic(state_path, new_state)
    print(f"Notifications sent: {len(successes)}; pending failures: {len(failures)}")
    if failures:
        raise DiscordError(f"{len(failures)} Discord notification(s) failed; retained in pending")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor PTT Gamesale for a target listing")
    parser.add_argument("--config", type=Path, default=Path("config/monitor.json"))
    parser.add_argument("--state", type=Path, default=Path("data/state.json"))
    parser.add_argument("--mode", choices=("normal", "dry-run", "test-discord"), default="normal")
    parser.add_argument("--now", help="Timezone-aware ISO-8601 time for tests or dry-run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(args.config, args.state, args.mode, args.now)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except PttFetchError as exc:
        print(f"PTT ERROR: {exc}", file=sys.stderr)
        return 3
    except DiscordError as exc:
        print(f"DISCORD ERROR: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

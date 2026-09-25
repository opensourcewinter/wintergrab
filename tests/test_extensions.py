"""Downloader middleware, item pipelines, crawl order and priority functions."""

from __future__ import annotations

import asyncio
import json

import pytest

from wintergrab import Request, Response, Spider
from wintergrab.errors import CheckpointError, ConfigurationError
from wintergrab.spider.middleware import DropItem, IgnoreRequest, ItemPipeline


class Base(Spider):
    log_level = None
    obey_robots_txt = False
    autothrottle = False


# --------------------------------------------------------------------------- #
# middleware
# --------------------------------------------------------------------------- #
def test_request_middleware_can_answer_replace_or_ignore(fresh_site) -> None:
    calls: list[str] = []

    class Mock:
        def process_request(self, request, spider):
            calls.append("mock:" + request.url.rsplit("/", 1)[-1])
            if request.url.endswith("/mocked"):
                return Response(request.url, status=200, body=b"<h1>from middleware</h1>", request=request)
            if request.url.endswith("/old"):
                return Request(fresh_site.url + "/product/2", callback="parse")
            if request.url.endswith("/skip"):
                raise IgnoreRequest("not today")
            return None

    class Mocked(Base):
        middlewares = [Mock()]
        start_urls = [fresh_site.url + p for p in ("/mocked", "/old", "/skip", "/product/1")]

        def parse(self, response):
            yield {"url": response.url, "h1": response.css("h1::text").get()}

    result = Mocked(concurrency=1).run()
    by_url = {item["url"]: item["h1"] for item in result.items}
    assert by_url == {
        fresh_site.url + "/mocked": "from middleware",
        fresh_site.url + "/product/2": "Product 2",
        fresh_site.url + "/product/1": "Product 1",
    }
    hits = fresh_site.site.hits
    assert hits["/mocked"] == 0 and hits["/old"] == 0 and hits["/skip"] == 0
    stats = result.stats
    assert stats["middleware_responses"] == 1 and stats["rerouted"] == 1 and stats["ignored"] == 1
    assert "mock:2" in calls  # the replacement went through the middlewares too


def test_middleware_order_and_async_hooks(site) -> None:
    order: list[str] = []

    class Tracer:
        def __init__(self, name: str) -> None:
            self.name = name

        async def process_request(self, request, spider):
            await asyncio.sleep(0)
            order.append(f"req:{self.name}")

        def process_response(self, request, response, spider):
            order.append(f"resp:{self.name}")
            response.headers["X-Seen-By"] = response.headers.get("X-Seen-By", "") + self.name
            return response

    class Traced(Base):
        middlewares = [Tracer("A"), Tracer("B")]
        start_urls = [site.url + "/product/1"]

        def parse(self, response):
            yield {"seen": response.headers["X-Seen-By"]}

    result = Traced().run()
    assert order == ["req:A", "req:B", "resp:B", "resp:A"]  # the first middleware wraps the others
    assert result.items == [{"seen": "BA"}]


def test_response_middleware_can_request_something_else(fresh_site) -> None:
    class Relogin:
        def process_response(self, request, response, spider):
            if response.status == 403 and not request.meta.get("relogged"):
                return Request(fresh_site.url + "/product/3", meta={"relogged": True})
            return response

    class Guarded(Base):
        middlewares = [Relogin()]
        start_urls = [fresh_site.url + "/status/403"]

        def parse(self, response):
            yield {"url": response.url, "relogged": response.meta.get("relogged")}

    result = Guarded().run()
    assert result.items == [{"url": fresh_site.url + "/product/3", "relogged": True}]
    assert result.stats["rerouted"] == 1


def test_exception_middleware_can_recover(fresh_site) -> None:
    class Fallback:
        def process_exception(self, request, error, spider):
            if "unreachable" in request.url:
                return Response(request.url, status=200, body=b"<p id=x>offline copy</p>", request=request)
            return None  # default handling

    class Recover(Base):
        middlewares = [Fallback()]
        retries = 0
        start_urls = ["http://127.0.0.1:9/unreachable", "http://127.0.0.1:9/other"]
        errors: list[str] = []

        def parse(self, response):
            yield {"text": response.css("#x::text").get()}

        def on_error(self, request, error):
            self.errors.append(request.url)

    result = Recover().run()
    assert result.items == [{"text": "offline copy"}]
    assert Recover.errors == ["http://127.0.0.1:9/other"]


def test_broken_middleware_fails_the_request_not_the_crawl(site) -> None:
    class Broken:
        def process_request(self, request, spider):
            if request.url.endswith("/1"):
                raise RuntimeError("bug")
            if request.url.endswith("/2"):
                return "not a response"
            return None

    class Crawl(Base):
        middlewares = [Broken()]
        start_urls = [site.url + f"/product/{i}" for i in (1, 2, 3)]

        def parse(self, response):
            yield {"url": response.url}

        def on_error(self, request, error):
            pass

    result = Crawl().run()
    assert [i["url"] for i in result.items] == [site.url + "/product/3"]
    assert result.stats["middleware_errors"] == 2 and result.stats["failed"] == 2


def test_objects_without_hooks_are_rejected(site) -> None:
    class Nothing(Base):
        middlewares = [object()]
        start_urls = [site.url + "/product/1"]

    with pytest.raises(ConfigurationError) as info:
        Nothing().run()
    assert info.value.key == "middlewares"

    class NoPipe(Base):
        pipelines = [42]
        start_urls = [site.url + "/product/1"]

    with pytest.raises(ConfigurationError):
        NoPipe().run()


# --------------------------------------------------------------------------- #
# pipelines
# --------------------------------------------------------------------------- #
def test_pipelines_transform_drop_and_close(site, tmp_path) -> None:
    events: list[str] = []

    class Price(ItemPipeline):
        def open_spider(self, spider):
            events.append("open")

        def process_item(self, item, spider):
            item["price"] = float(item["price"].strip("$"))
            return item

        def close_spider(self, spider):
            events.append("close")

    class Cheap:
        async def process_item(self, item, spider):
            if item["price"] > 8:
                raise DropItem("too expensive")
            return item

    def tag(item):
        return {**item, "checked": True}

    out_file = tmp_path / "items.jsonl"

    class Shop(Base):
        pipelines = [Price(), Cheap(), tag]
        start_urls = [site.url + f"/product/{i}" for i in range(1, 8)]
        output = str(out_file)

        def parse(self, response):
            yield {"name": response.css("h1::text").get(), "price": response.css(".price::text").get()}

    result = Shop().run()
    assert events == ["open", "close"]
    names = sorted(i["name"] for i in result.items)
    assert names == ["Product 1", "Product 2"]  # 6.25 and 7.50 pass; 8.75+ dropped
    assert all(i["checked"] is True and isinstance(i["price"], float) for i in result.items)
    stats = result.stats
    assert stats["items_dropped/Cheap"] == 5 and stats["items_dropped"] == 5
    written = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["name"] for r in written) == names  # the exporter saw the pipeline output


def test_pipeline_errors_drop_the_item(site) -> None:
    def boom(item):
        raise RuntimeError("pipeline bug")

    class Crawl(Base):
        pipelines = [boom]
        start_urls = [site.url + "/product/1"]

        def parse(self, response):
            yield {"x": 1}

    result = Crawl().run()
    assert result.items == [] and result.stats["pipeline_errors"] == 1
    assert result.stats["items_dropped/boom"] == 1


# --------------------------------------------------------------------------- #
# crawl order and priorities
# --------------------------------------------------------------------------- #
def _tree_order(site, tmp_path, **settings) -> list[str]:
    class Tree(Base):
        start_urls = [site.url + "/tree/"]
        concurrency = 1

        def parse(self, response):
            yield {"node": response.css("#node::text").get()}
            yield from response.follow_all("a.child")

    return [i["node"] for i in Tree(**settings).run().items]


BFS = ["root", "a", "b", "a/a", "a/b", "b/a", "b/b"]
DFS = ["root", "b", "b/b", "b/a", "a", "a/b", "a/a"]


def test_breadth_and_depth_first(site, tmp_path) -> None:
    assert _tree_order(site, tmp_path) == BFS
    assert _tree_order(site, tmp_path, crawl_order="dfs") == DFS
    disk = {"frontier": "disk", "crawl_dir": str(tmp_path / "dfs")}
    assert _tree_order(site, tmp_path, crawl_order="dfs", **disk) == DFS
    assert _tree_order(site, tmp_path, crawl_order="bfs", frontier="disk", crawl_dir=str(tmp_path / "bfs")) == BFS
    with pytest.raises(ConfigurationError):
        _tree_order(site, tmp_path, crawl_order="random")


def test_disk_frontier_keeps_its_order(site, tmp_path) -> None:
    class Paused(Base):
        start_urls = [site.url + "/tree/"]
        concurrency = 1
        frontier = "disk"
        crawl_dir = str(tmp_path / "crawl")
        max_pages = 1

        def parse(self, response):
            yield from response.follow_all("a.child")

    assert Paused(crawl_order="dfs").run().status == "limit"
    with pytest.raises(CheckpointError, match="crawl_order"):
        Paused(crawl_order="bfs", max_pages=5).run()


def test_dfs_order_survives_pause_and_resume_in_memory(site, tmp_path) -> None:
    visited: list[str] = []

    class Tree(Base):
        start_urls = [site.url + "/tree/"]
        concurrency = 1
        crawl_order = "dfs"
        crawl_dir = str(tmp_path / "mem")

        def parse(self, response):
            visited.append(response.css("#node::text").get())
            yield from response.follow_all("a.child")

    assert Tree(max_pages=3).run().status == "limit"
    Tree(max_pages=100).run()
    assert visited == DFS


def by_letter(request):
    return 10 if request.url.endswith("/b") else 0


def test_priority_functions(site) -> None:
    class Prioritized(Base):
        start_urls = [site.url + "/tree/"]
        concurrency = 1
        max_depth = 1
        priority_fn = by_letter  # a plain function stored on the class

        def parse(self, response):
            yield {"node": response.css("#node::text").get()}
            yield from response.follow_all("a.child")

    assert [i["node"] for i in Prioritized().run().items] == ["root", "b", "a"]
    by_instance = Prioritized(priority_fn=lambda r: 5 if r.url.endswith("/a") else 0)
    assert [i["node"] for i in by_instance.run().items] == ["root", "a", "b"]

    class Method(Prioritized):
        def priority_fn(self, request):
            return by_letter(request)

    assert [i["node"] for i in Method().run().items] == ["root", "b", "a"]

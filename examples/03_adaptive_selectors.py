"""Adaptive selectors keep working after a site redesign. Runs offline.

The first run remembers what `.product` and `.product .price` matched.
Then the "site" ships a redesign: new class names, extra wrappers, new
items. The old selectors match nothing - but with ``adaptive=True`` wintergrab
finds the most similar elements and carries on.

    python examples/03_adaptive_selectors.py
"""

import wintergrab as wg

OLD = """
<html><body>
  <h1 id="title">Summer sale</h1>
  <div id="products">
    <div class="product"><h2>Blue mug</h2><span class="price">$12</span><a href="/p/1">Buy</a></div>
    <div class="product"><h2>Red mug</h2><span class="price">$9</span><a href="/p/2">Buy</a></div>
  </div>
</body></html>
"""

NEW = """
<html><body>
  <header><h1 class="headline">Summer sale</h1></header>
  <main><section class="catalog">
    <div class="product-card"><h2>Green mug</h2><div class="meta"><span class="price-tag">$11</span></div><a href="/p/3">Buy</a></div>
    <div class="product-card"><h2>Blue mug</h2><div class="meta"><span class="price-tag">$12</span></div><a href="/p/1">Buy</a></div>
    <div class="product-card"><h2>Tea pot</h2><div class="meta"><span class="price-tag">$30</span></div><a href="/p/4">Buy</a></div>
  </section></main>
</body></html>
"""


def main() -> dict:
    # Keep fingerprints in memory for the demo. By default they go to a small
    # SQLite file in your cache directory, so they survive between runs.
    storage = wg.MemoryStorage()
    url = "https://shop.example/sale"  # fingerprints are stored per domain

    before = wg.parse(OLD, url=url, adaptive_storage=storage)
    print("before:", before.css(".product h2::text", adaptive=True).getall())
    print("        ", before.css(".product .price::text", adaptive=True).getall())
    print("        ", before.css("h1#title::text", adaptive=True).get())

    after = wg.parse(NEW, url=url, adaptive_storage=storage)
    print("plain selector after redesign:", after.css(".product h2::text").getall())
    names = after.css(".product h2::text", adaptive=True).getall()
    prices = after.css(".product .price::text", adaptive=True).getall()
    title = after.css("h1#title::text", adaptive=True).get()
    print("adaptive after redesign:", names, prices, title)

    # Bonus: no selector at all - find one item by its text, then its lookalikes.
    first = after.find_by_text("Green mug").first.parent
    print("find_similar:", [card.css("h2::text").get() for card in [first, *first.find_similar()]])
    return {"names": names, "prices": prices, "title": title}


if __name__ == "__main__":
    main()

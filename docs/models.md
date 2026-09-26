# Language models

wintergrab needs no model. Its extraction reads structured data, meta tags,
your selectors, labels and page layout. A model is one more source of
values, used only when you name one and only for what the page's own data
does not give.

```bash
wintergrab get https://shop.example/p/1 --extract shoe.schema.json --model ollama:NAME
wintergrab crawl https://shop.example/ --extract shoe.schema.json --model openai:NAME -o shoes.jsonl
```

```python
from wintergrab.extraction import Extractor
from wintergrab.models import load_model

extractor = Extractor("shoe.schema.json", model=load_model("anthropic:NAME"))
```

`NAME` is the model's name as the provider knows it.

| `--model` | API | Key |
|---|---|---|
| `openai:NAME` | OpenAI's chat completions API, or any server that speaks it (vLLM, llama.cpp, LM Studio...: `--model-url http://host:8000/v1`) | `OPENAI_API_KEY` (a server of your own may need none) |
| `anthropic:NAME` | The Anthropic Messages API | `ANTHROPIC_API_KEY` |
| `ollama:NAME` | A local Ollama server (`http://localhost:11434`, or `--model-url`) | none |

## What a model is asked, and what is done with its answers

For each page, the extractor first uses its own strategies. It then asks
the model, in one request, for the fields still missing or found with less
than 50% confidence (`model_threshold`). The request holds:

- the fields wanted, with their types and descriptions;
- what was already found, as context;
- the page as Markdown;
- with `vision=True` (`--vision`), the page's screenshot.

The answer is not taken on trust. It is read with each field's type,
validated, and compared with what other strategies found. It is also
checked against the page:

- a value that appears on the page keeps its confidence;
- a number written another way on the page loses a little;
- a value that appears nowhere on the page is kept aside, with the note
  `not-on-page`. It becomes an alternative, never the answer;
- except when the model was shown the screenshot: a value in no text may be
  drawn there (a chart). It is kept with a low confidence and the note
  `image-only` (see [visual](visual.md#screenshots-for-models-that-read-images)).

Models can invent values; the page cannot.

```python
record = extractor.extract(page)
record.fields["material"].method     # "model"
record.fields["material"].source     # "model:openai:NAME"
record.fields["material"].notes      # [] when the page says it, ["not-on-page", ...] when not
```

A model that fails (a network error, a 5xx, a 429) is asked again after 2
and 8 seconds, and after that the page is extracted without it. The crawl
logs what the model used: `openai:NAME: 212 request(s), 380,114 input and
6,020 output tokens`.

## Reading goal requests

`wintergrab goal "..." --model PROVIDER:NAME` has the model read the request.
It gives the kind of record, the fields, the part of the site, the
conditions, a limit, and whether to keep watching, and all of it is
checked: see [goals](goals.md#what-a-request-can-say). The crawl that
follows extracts without the model.

## Generating scrapers

`wintergrab generate "..." --model PROVIDER:NAME` asks the model, on a few
sample pages, for the fields the pages do not publish for machines. From
its answers (only those the pages hold) it learns selectors, so the
scraper reads those fields on every other page without the model. The
report says how many tokens that took. See [generated scrapers](generate.md).

## Keys and what leaves your machine

- **Keys** come from the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)
  or `load_model(..., api_key=...)`. They are never written into run
  records, logs or the dashboard.
- **What is sent**: the page's text, and images if you give them (its
  screenshot with `--vision`). It goes
  to the API you name. For pages you may not send to a third party, run a
  model locally (`ollama:NAME`, or your own server with `--model-url`).
- **Cost**: a crawl asks the model at most once per page, and only for
  pages missing fields. On a large crawl that is still many requests: try
  `--max-pages` first.

## In code

```python
from wintergrab.models import Image, load_model

model = load_model("openai:NAME", base_url="http://localhost:8000/v1", timeout=60, max_tokens=512)
model.complete("Summarize this page: ...", system="Be brief.")        # text
model.complete("What does the chart show?", images=[Image(png_bytes, "image/png")])
model.usage                                                            # {"input": ..., "output": ..., "requests": ...}
```

A provider is a `ModelProvider` subclass with a `complete()` method. Add
yours with a [plugin](plugins.md) (`registry.model_provider("name", Class)`),
or pass any function to `Extractor(model=...)`: it gets a `ModelRequest`
and returns `{field: value}` (see [extraction](extraction.md#extraction-models)).

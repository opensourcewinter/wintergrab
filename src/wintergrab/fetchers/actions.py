"""Browser actions: what to do on a page before it is read, written as data rather than code.

::

    browser.get(url, actions=[
        "dismiss #cookie-banner button",          # a dialog in the way, if there is one
        "click button.load-more until-gone",      # "load more" until there is no more
        "expand .faq button",                     # every accordion open
        {"fill": {"input[name=q]": "parka"}},     # a form
        "press Enter",
        "wait .results",
        "scroll 5",                               # lazy loading
        "tabs .tabs a",                           # each tab's content kept (response.snapshots)
        "screenshot shot.png", "pdf page.pdf",
        "download a.export",                      # a file the page offers (response.downloads)
    ])
    response.actions      # what each step did: [{"step": "click button.load-more until-gone", "ok": True,
                          #   "detail": "clicked 4 time(s); it is gone"}, ...]

In a spider, ``Request(url, session="browser", options={"actions": [...]})``; on the command line,
``wintergrab get URL --do "click .more until-gone" --do "wait .item"``; in a file (JSON or YAML), a
list of the same steps (``--actions FILE``).

A step is a string, ``"VERB [TARGET] [ARGUMENT]"``, or a one-key mapping ``{VERB: ...}``:

=================  ================================================================================
``click S``        click the first element matching ``S``. ``click S xN`` clicks up to ``N`` times
                   while it is there; ``click S until-gone`` until it disappears (at most 50 times)
``expand S``       click every visible element matching ``S`` once (accordions, "read more" toggles)
``dismiss S``      click ``S`` if it is there (a cookie dialog, a modal); nothing if not
``hover S``        move the pointer over ``S`` (menus that open on hover)
``fill S => V``    type ``V`` into the field ``S`` (``{"fill": {S: V, ...}}`` fills several)
``select S => V``  choose the option ``V`` (its value or label) of the list ``S``
``check S``        tick the box ``S`` (``uncheck S`` clears it)
``press K``        press the key ``K`` (``Enter``, ``Escape``); ``press S => K`` in the field ``S``
``wait S``         wait for ``S`` to appear; ``wait 1.5`` waits that many seconds
``scroll N``       scroll to the bottom, up to ``N`` times, while the page grows (lazy loading)
``tabs S``         click each element matching ``S`` in turn, keeping the page's HTML after each in
                   ``response.snapshots`` (tabbed content that replaces itself)
``snapshot``       keep the page's HTML as it is now in ``response.snapshots``
``screenshot F``   save a full-page PNG to the file ``F``
``pdf F``          save the page as a PDF to ``F`` (Chromium, headless)
``download S``     click ``S`` and keep the file it downloads (``response.downloads``; a directory
                   given with ``downloads=``, else a temporary one)
=================  ================================================================================

A step that cannot be done (no element to click, a wait that times out) stops the page with a
:class:`~wintergrab.errors.BrowserFetchError` naming the step, not retried, except ``dismiss`` and steps
marked ``optional`` (``{"click": "S", "optional": true}``), which are noted and passed over. A step that
leads to a page that does not load stops the page, optional or not; the browser fetcher's network policy
holds for every request a step makes. There is no step that runs a script: actions do what a person could
do on the page.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import BrowserError, BrowserFetchError, ConfigurationError

__all__ = ["VERBS", "Action", "ActionsResult", "load_actions", "parse_actions", "run_actions"]

#: Every verb, and whether it takes a selector.
VERBS: dict[str, bool] = {
    "click": True, "expand": True, "dismiss": True, "hover": True, "fill": True, "select": True, "check": True,
    "uncheck": True, "press": False, "wait": False, "scroll": False, "tabs": True, "snapshot": False,
    "screenshot": False, "pdf": False, "download": True,
}  # fmt: skip
MAX_REPEAT = 50  # clicks of one "until-gone" step
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_REPEAT = re.compile(r"\s+(?:x(\d+)|until-gone)$")
_SEPARATOR = " => "


@dataclass(frozen=True)
class Action:
    """One step (see the module docs).

    Attributes:
        verb: What to do.
        target: The selector (or the key, seconds, times, file, by verb).
        value: What to type or choose (``fill``, ``select``; the key for ``press S => K``).
        repeat: How many times at most (``click S xN``); 1 by default.
        until_gone: Click until the element disappears.
        optional: Pass over the step if it cannot be done.
    """

    verb: str
    target: str = ""
    value: str | None = None
    repeat: int = 1
    until_gone: bool = False
    optional: bool = False

    def __str__(self) -> str:
        text = f"{self.verb} {self.target}".strip()
        if self.value is not None:
            text += f"{_SEPARATOR}{self.value}"
        if self.until_gone:
            text += " until-gone"
        elif self.repeat > 1:
            text += f" x{self.repeat}"
        return text + (" (optional)" if self.optional and self.verb != "dismiss" else "")  # dismiss always is


def parse_actions(steps: Iterable[str | Mapping[str, Any]] | str | Mapping[str, Any]) -> list[Action]:
    """Steps (strings or one-key mappings; see the module docs) as :class:`Action` s.

    Raises:
        ConfigurationError: A step that does not read as one.
    """
    if isinstance(steps, (str, Mapping)):
        steps = [steps]
    out: list[Action] = []
    for step in steps:
        out.extend(_parse_step(step))
    return out


def load_actions(path: str | Path) -> list[Action]:
    """Steps from a JSON or YAML file holding a list of them."""
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith((".yaml", ".yml")):
        from ..files import yaml_module

        data = yaml_module().safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}: expected a list of browser actions", key="actions")
    return parse_actions(data)


def _parse_step(step: str | Mapping[str, Any]) -> list[Action]:
    if isinstance(step, Action):
        return [step]
    if isinstance(step, str):
        return [_from_text(step)]
    if not isinstance(step, Mapping) or not step:
        raise ConfigurationError(f"a browser action is a string or a mapping, not {step!r}", key="actions")
    options = dict(step)
    optional = bool(options.pop("optional", False))
    until_gone = bool(options.pop("until_gone", False))
    repeat = options.pop("repeat", 1)
    if len(options) != 1:
        raise ConfigurationError(f"a browser action has one verb: {step!r}", key="actions")
    verb, argument = next(iter(options.items()))
    verb = str(verb).lower()
    _check_verb(verb, step)
    if verb in ("fill", "select") and isinstance(argument, Mapping):
        return [Action(verb, str(s), str(v), optional=optional) for s, v in argument.items()]
    if verb == "press" and isinstance(argument, Mapping):
        return [Action(verb, str(s), str(k), optional=optional) for s, k in argument.items()]
    if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= MAX_REPEAT:
        raise ConfigurationError(f"repeat: a number from 1 to {MAX_REPEAT}, not {repeat!r}", key="actions")
    text = "" if argument is None or argument is True else str(argument)
    action = _from_text(f"{verb} {text}".strip())
    return [Action(action.verb, action.target, action.value, max(action.repeat, repeat),
                   action.until_gone or until_gone, optional or action.optional)]  # fmt: skip


def _from_text(text: str) -> Action:
    stripped = " ".join(text.split())
    verb, _, rest = stripped.partition(" ")
    verb = verb.lower()
    _check_verb(verb, text)
    repeat, until_gone = 1, False
    match = _REPEAT.search(rest) if verb == "click" else None
    if match:
        rest = rest[: match.start()]
        until_gone = match.group(1) is None
        repeat = MAX_REPEAT if until_gone else int(match.group(1))
        if not 1 <= repeat <= MAX_REPEAT:
            raise ConfigurationError(f"{text!r}: x1 to x{MAX_REPEAT}", key="actions")
    target, value = rest, None
    if _SEPARATOR.strip() in rest and verb in ("fill", "select", "press"):
        target, _, value = rest.partition(_SEPARATOR.strip())
        target, value = target.strip(), value.strip()
    if verb in ("fill", "select") and value is None:
        raise ConfigurationError(f"{text!r}: write {verb} SELECTOR => VALUE", key="actions")
    if VERBS[verb] and not target:
        raise ConfigurationError(f"{text!r}: {verb} needs a selector", key="actions")
    if verb in ("screenshot", "pdf") and not target:
        raise ConfigurationError(f"{text!r}: {verb} needs a file to save to", key="actions")
    if verb == "press" and not target:
        raise ConfigurationError(f"{text!r}: press needs a key (Enter, Escape...)", key="actions")
    if verb == "scroll" and target and not target.isdigit():
        raise ConfigurationError(f"{text!r}: scroll takes a number of times", key="actions")
    return Action(verb, target, value, repeat, until_gone, verb == "dismiss")


def _check_verb(verb: str, step: Any) -> None:
    if verb not in VERBS:
        raise ConfigurationError(
            f"unknown browser action {verb!r} in {step!r}; known: {', '.join(VERBS)}", key="actions"
        )


@dataclass
class ActionsResult:
    """What the steps did: a log, and what they kept."""

    log: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, str]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)


async def run_actions(
    page: Any,
    actions: Sequence[Action],
    *,
    timeout: float = 30.0,
    downloads: str | Path | None = None,
) -> ActionsResult:
    """Do ``actions`` on a Playwright page, in order (see the module docs); ``timeout`` in seconds per step.

    Raises:
        BrowserFetchError: A step that could not be done and is not optional (not retried: trying again would
            not make it possible).
    """
    result = ActionsResult()
    timeout_ms = timeout * 1000
    for action in actions:
        url = page.url
        try:
            detail = await _run(page, action, timeout_ms, downloads, result)
            if _error_page(page):  # a link to where nothing loads, or where the network policy refuses to go
                raise BrowserError("the page it led to could not be loaded")
        except Exception as exc:
            reason = " ".join(str(exc).split("\n")[0].split())[:300]
            result.log.append({"step": str(action), "ok": False, "detail": reason})
            if not action.optional or _error_page(page):
                raise BrowserFetchError(
                    url,
                    f"browser action {str(action)!r} failed: {reason}",
                    retryable=False,
                    kind="browser",
                    context={"action": str(action)},
                ) from None
            continue
        result.log.append({"step": str(action), "ok": True, "detail": detail})
    return result


def _error_page(page: Any) -> bool:
    """The browser shows its own error page: the page's address did not load."""
    return str(page.url).startswith("chrome-error:")


async def _run(page: Any, action: Action, timeout_ms: float, downloads: Any, result: ActionsResult) -> str:
    verb, target = action.verb, action.target
    if verb == "click":
        clicks = 0
        for _ in range(action.repeat):
            locator = page.locator(target).first
            if clicks and not await _present(locator):
                break
            await locator.click(timeout=timeout_ms)
            clicks += 1
            await _settle(page)
        gone = not await _present(page.locator(target).first)
        if action.until_gone and not gone:
            return f"clicked {clicks} times (the most); it is still there"
        repeated = action.until_gone or action.repeat > 1
        return f"clicked {clicks} time(s)" + ("; it is gone" if gone and repeated else "")
    if verb == "expand":
        count, clicked = await page.locator(target).count(), 0
        for i in range(count):
            element = page.locator(target).nth(i)
            if await element.is_visible():
                await element.click(timeout=timeout_ms)
                clicked += 1
        await _settle(page)
        return f"clicked {clicked} element(s)" + (f" ({count - clicked} hidden)" if clicked < count else "")
    if verb == "dismiss":
        element = page.locator(target).first
        if not await _present(element):
            return "not there"
        await element.click(timeout=timeout_ms)
        await _settle(page)
        return "clicked"
    if verb == "hover":
        await page.locator(target).first.hover(timeout=timeout_ms)
        return "hovered"
    if verb == "fill":
        await page.locator(target).first.fill(action.value or "", timeout=timeout_ms)
        return f"typed {len(action.value or '')} character(s)"
    if verb == "select":
        field_ = page.locator(target).first
        try:
            await field_.select_option(value=action.value, timeout=min(timeout_ms, 5000))
        except Exception:
            await field_.select_option(label=action.value, timeout=timeout_ms)
        return f"chose {action.value!r}"
    if verb in ("check", "uncheck"):
        box = page.locator(target).first
        await (box.check if verb == "check" else box.uncheck)(timeout=timeout_ms)
        return "done"
    if verb == "press":
        if action.value is not None:
            await page.locator(target).first.press(action.value, timeout=timeout_ms)
        else:
            await page.keyboard.press(target)
        await _settle(page)
        return "pressed"
    if verb == "wait":
        if re.fullmatch(r"\d+(?:\.\d+)?", target):
            await page.wait_for_timeout(min(float(target), 120.0) * 1000)
            return f"waited {target} s"
        await page.locator(target).first.wait_for(state="visible", timeout=timeout_ms)
        return "there"
    if verb == "scroll":
        times, grew = int(target or 10), 0
        last = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
        for _ in range(max(1, times)):
            await page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
            await page.wait_for_timeout(600)
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            if height == last:
                break
            last, grew = height, grew + 1
        return f"the page grew {grew} time(s)"
    if verb == "tabs":
        count = await page.locator(target).count()
        for i in range(count):
            tab = page.locator(target).nth(i)
            label = " ".join((await tab.inner_text(timeout=timeout_ms)).split())[:80]
            await tab.click(timeout=timeout_ms)
            await _settle(page)
            result.snapshots.append({"after": f"{target} #{i + 1}: {label}", "html": await page.content()})
        return f"{count} tab(s) kept"
    if verb == "snapshot":
        result.snapshots.append({"after": f"step {len(result.log) + 1}", "html": await page.content()})
        return "kept"
    if verb == "screenshot":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=target, full_page=True)
        return f"saved {target}"
    if verb == "pdf":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        await page.pdf(path=target)
        return f"saved {target}"
    if verb == "download":
        folder = Path(downloads) if downloads is not None else Path(tempfile.mkdtemp(prefix="wintergrab-downloads-"))
        folder.mkdir(parents=True, exist_ok=True)
        async with page.expect_download(timeout=timeout_ms) as waiting:
            await page.locator(target).first.click(timeout=timeout_ms)
        download = await waiting.value
        name = Path(download.suggested_filename or "").name  # no directories from the site
        if name in ("", ".", ".."):
            name = "download"
        path = _free(folder / name)
        await download.save_as(str(path))
        size = path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            path.unlink()
            raise BrowserError(f"the download {name} is larger than {MAX_DOWNLOAD_BYTES // 2**20} MB")
        result.downloads.append({"url": download.url, "path": str(path), "name": name, "bytes": size})
        return f"saved {path} ({size:,} bytes)"
    raise AssertionError(f"unhandled verb {verb}")  # pragma: no cover - VERBS lists every verb handled


async def _present(locator: Any) -> bool:
    try:
        return bool(await locator.count()) and bool(await locator.is_visible())
    except Exception:
        return False


async def _settle(page: Any) -> None:
    """Let what a step started finish: requests it made, a navigation (a second at most)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=1000)
    except Exception:
        pass


def _free(path: Path) -> Path:
    """``path``, or ``name (2).ext``... when it exists already."""
    if not path.exists():
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise BrowserError(f"too many downloads named {path.name}")

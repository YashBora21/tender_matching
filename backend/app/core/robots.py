"""robots.txt matching, including the rules the standard library throws away (D-117).

`urllib.robotparser` ignores a rule that carries a wildcard: `Disallow: *.pdf`
- the way a manufacturer says its catalogues are not for crawling - is read as
nothing at all, and the document is fetched anyway. Every document this project
holds was collected under the promise that a host's robots.txt is obeyed (FR-4),
so the matching lives here: `*` is any sequence, a `$` anchors the end, and a
rule with no `$` decides by prefix, which is the reading the sites themselves
are written to and the more careful of the two.

Both fetch paths use it - the collector's and the fence in `core.fetching` - so
there is one answer to "did this host allow this document".
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Rule:
    """One Allow or Disallow line, as a matcher and the way it was written."""

    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @property
    def length(self) -> int:
        return len(self.pattern)


def _rule(allow: bool, value: str) -> Optional[Rule]:
    """Compile a rule's path, or nothing when the path says nothing.

    A bare `Disallow:` is the group's way of restricting nothing, and an empty
    `Allow:` carves nothing out, so neither is a rule.
    """
    if not value:
        return None
    anchored = value.endswith("$")
    body = value[:-1] if anchored else value
    parts = [re.escape(part) for part in body.split("*")]
    regex = re.compile("^" + ".*".join(parts) + ("$" if anchored else ""))
    return Rule(allow=allow, pattern=value, regex=regex)


def _groups(text: str) -> list[tuple[list[str], list[Rule]]]:
    """The file as groups: the agents each one names, then its rules.

    A group runs from its `User-agent:` lines to the next group. Anything the
    file carries that is not a rule (`Sitemap`, `Crawl-delay`) is left alone,
    and rules before any agent are read as the `*` group's, because a file that
    starts with a ban means it.
    """
    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        field, _, value = line.partition(":")
        name = field.strip().lower()
        value = value.strip()
        if name == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif name in ("allow", "disallow"):
            rule = _rule(allow=name == "allow", value=value)
            if rule is None:
                continue
            if not agents:
                agents = ["*"]
            rules.append(rule)

    if agents or rules:
        groups.append((agents, rules))
    return groups


def _applies(group_agents: list[str], user_agent: str) -> Optional[int]:
    """How specifically this group names that user agent, or None if it does not.

    The group whose token is inside the agent string is the one that meant it;
    `*` is every agent and so is the weakest match. A token is matched as a
    substring rather than a prefix, because a site that writes `bot` means
    `tender-corpus-collector` too, and reading it narrowly would fetch what the
    site banned.
    """
    ua = user_agent.lower()
    best: Optional[int] = None
    for agent in group_agents:
        if not agent or agent == "*":
            specificity = 0
        elif agent in ua:
            specificity = len(agent)
        else:
            continue
        if best is None or specificity > best:
            best = specificity
    return best


def deciding_rule(text: str, user_agent: str, url: str) -> Optional[Rule]:
    """The rule that decides this URL for this agent, or None when none does.

    The longest matching path wins. An Allow of the same length wins the tie: a
    site that bans a tree and then names one path inside it means that path.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    best_group: Optional[list[Rule]] = None
    best_specificity: Optional[int] = None
    for group_agents, rules in _groups(text):
        specificity = _applies(group_agents, user_agent)
        if specificity is None:
            continue
        if best_specificity is None or specificity > best_specificity:
            best_specificity = specificity
            best_group = rules

    if best_group is None:
        return None

    winner: Optional[Rule] = None
    for rule in best_group:
        if not (rule.regex.match(path) or rule.regex.match(urllib.parse.unquote(path))):
            continue
        if winner is None or rule.length > winner.length:
            winner = rule
        elif rule.length == winner.length and rule.allow and not winner.allow:
            winner = rule
    return winner


def allows(url: str, text: str, user_agent: str) -> tuple[bool, Optional[str]]:
    """Whether the robots.txt text lets that agent fetch that URL, and the note.

    Silence is not a restriction, and an allowance needs no note: the only
    answer worth recording is the rule that stopped the fetch.
    """
    rule = deciding_rule(text, user_agent, url)
    if rule is None or rule.allow:
        return True, None
    return (
        False,
        "robots.txt disallows this path for the collector's user agent",
    )

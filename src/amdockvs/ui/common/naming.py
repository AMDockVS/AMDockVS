"""Name what a message is about: "4OYS, 4UWF and 2 more" instead of "4 receptor(s)"."""
from __future__ import annotations

from typing import Iterable

NAMED = 3  # how many names a message lists before "and N more"


def name_list(names: Iterable[str], total: int | None = None, *, limit: int = NAMED) -> str:
    """The first `limit` names, then how many of `total` are left: "4OYS, 4UWF and 4UWG",
    "4OYS, 4UWF, 4UWG and 2 more". "" when there is no name to give."""
    names = [str(name) for name in names if str(name or "").strip()]
    total = len(names) if total is None else max(int(total), len(names))
    shown = names[:limit]
    if not shown:
        return ""
    rest = total - len(shown)
    if rest > 0:
        return f"{', '.join(shown)} and {rest:,} more"
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def about(count: int, names: Iterable[str], noun: str, predicate: str, *,
          end: str = ".", sep: str = ": ") -> str:
    """A sentence about `count` things, naming them: "4OYS has no box." when it is one,
    "3 receptors have no box: 4OYS, 4UWF and 4UWG." when several. `predicate` is written for
    one ("has no box"); "has" becomes "have" for several. `end=""` gives a fragment to join,
    and `sep=":\\n"` puts the names on their own line, where a dialog has the room."""
    names = list(names)
    count = int(count)
    if count == 1 and names:
        return f"{names[0]} {predicate}{end}"
    plural = predicate.replace("has ", "have ", 1) if predicate.startswith("has ") else predicate
    listed = name_list(names, count)
    return f"{count:,} {noun}{'' if count == 1 else 's'} {plural}" + (f"{sep}{listed}{end}" if listed else end)


__all__ = ["NAMED", "about", "name_list"]

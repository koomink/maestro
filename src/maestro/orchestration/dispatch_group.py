"""Stable identity for one approval group inside one signal run.

An approval dispatch that dies partway has to be resumable, and resuming
means recognizing the groups it already sent.  A random ``approval_id``
cannot do that, so the group needs a name derived only from what the signal
package already says: the run it belongs to and the strategies whose orders
it carries.

The name is the whole scope, not a hash of it.  A short digest shares an
identification space between scopes that have nothing to do with each other,
and a collision there would let one group's resume adopt another group's
envelope -- approving orders nobody was shown.  A ``duplicate_key`` has no
length limit, so carrying the full scope costs nothing.
"""

import json
from collections.abc import Sequence

_PREFIX = "dispatch-group"


def dispatch_group_id(signal_run_id: str, source_strategy_ids: Sequence[str]) -> str:
    """Return the durable identity of one approval group.

    ``source_strategy_ids`` is order-insensitive and deduplicated: it names a
    set of strategies, and the orchestrator builds it by walking orders, so
    the same group can arrive with its members in any order.

    The strategy ids are serialized as a JSON array rather than joined,
    because a join has no escaping: with ``":"`` as the separator, the groups
    ``["a:b", "c"]`` and ``["a", "b:c"]`` would produce one string and share
    one envelope.  ``account_id`` and the other order-scope fields are
    deliberately absent -- the orchestrator keys groups by strategy ids alone
    (``_approval_order_groups``), so they add no identifying power, and the
    envelope's ``account_ids`` drops nulls, so including them here would let
    the same group serialize two ways.  They are verified on reuse instead.
    """
    if not isinstance(signal_run_id, str) or not signal_run_id:
        raise ValueError(f"signal_run_id must be a non-empty string: {signal_run_id!r}")
    if not source_strategy_ids:
        raise ValueError("An approval group must name at least one strategy")
    for strategy_id in source_strategy_ids:
        # The caller already coerces to str and drops falsy values. Refuse
        # rather than coerce: a quiet str() here would mint an id that the
        # run which wrote the envelope never produced, so no resume could
        # match it and the group would be dispatched a second time.
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError(
                f"source_strategy_ids must be non-empty strings: {strategy_id!r}"
            )
    scope = json.dumps(
        sorted(set(source_strategy_ids)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # No Unicode normalization. Two identifiers that are NFC-equivalent but
    # written differently come from different config entries and must not
    # share a group; normalizing would merge them.
    return f"{_PREFIX}:{signal_run_id}:{scope}"

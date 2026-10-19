"""Provider and follower bots: the Superalgos v2 signals network on top of sa_core.

Nautilus is imported lazily (``replay.collect_intents``, the ``Nautilus*Executor`` adapters' inner followers), so
everything else here imports and tests without it.
"""

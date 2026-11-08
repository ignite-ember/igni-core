"""Permission pipeline — ordered gates that run before a tool call.

Each gate is a :class:`~.stage.PermissionStage` whose ``evaluate``
returns a :data:`~.outcome.StageOutcome`; :class:`~.pipeline.PermissionPipeline`
runs them in the CC-compatible order: hooks first (so plugins can
allow/deny/ask), then the hard-coded safety lists (defense in depth),
then the 6-mode evaluator and Bypass's last check, which a hook's
``allow`` skips.
"""

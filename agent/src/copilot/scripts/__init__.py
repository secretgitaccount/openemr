"""Operational scripts run by the CI gate and git hooks (PRP-12).

These modules are invoked as ``python -m copilot.scripts.<name>`` from the
``.githooks/pre-push`` hook and the ``make ci`` target — they are the executable
guardrails, not part of the request-serving path.
"""

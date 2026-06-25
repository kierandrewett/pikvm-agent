"""Curated Atlas page templates (memory/<topic>.md style).

Markdown skeletons with ``str.format`` placeholders. The exporter fills these;
they intentionally contain no secrets, screenshot paths, or verbatim typed text.
"""

from __future__ import annotations

PLAYBOOK_TEMPLATE = """\
# {title}

> Durable PiKVM playbook distilled from a completed session. Safe to reuse:
> contains no secrets, screenshots, credentials, or verbatim typed text.

## Task

{task}

## Summary

{summary}

## What worked

{steps}

## What blocked / needed a human

{blocked}

## Durable lessons

{lessons}
"""

INCIDENT_TEMPLATE = """\
# {title}

> Quick-capture incident from a PiKVM session. Redacted by construction.

## Task

{task}

## What happened

{summary}

## Signals

{steps}

## Durable lessons

{lessons}
"""

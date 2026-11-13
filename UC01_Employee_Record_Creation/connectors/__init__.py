"""Inbound/outbound connectors for UC-01.

A connector is transport only: it moves raw messages between an external
mailbox/queue and the agent. It never interprets business meaning and
never writes to a system of record. Parsing and all UC-01 decisions stay
in `src/sub_agent/intake/email_intake.py`.
"""

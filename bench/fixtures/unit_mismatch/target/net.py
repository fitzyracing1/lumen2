"""Retry scheduling."""

from clock import wait_budget


def retry_delays(attempts, timeout_seconds):
    """Split the timeout evenly across attempts. Delays are in seconds."""
    budget = wait_budget(timeout_seconds)
    return [budget / attempts] * attempts

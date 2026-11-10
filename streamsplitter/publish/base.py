"""What every destination (Slack, YouTube) shares: the job it is handed and the two ways a send can fail.

A destination never retries on its own beyond one request: it raises TransientError and the outbox (outbox.py) tries
again later, across network drops and restarts. PermanentError means trying again will not help (a revoked token, a
deleted channel, a file that is gone); the outbox stops and the notifier tells a person.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol


class TransientError(Exception):
    """Try again later: no network, a timeout, HTTP 429/5xx. `retry_after` (seconds) when the service said so."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """Trying again will not help without a person fixing something (log in again, invite the bot, ...)."""


@dataclass
class Match:
    """The match a clip shows, as far as it is known when the clip is written."""
    key: str | None                 # "2026mttd_qm12"; None when the clip could not be named
    name: str                       # the clip's file stem: "2026mttd_qm12", "2026mttd_qm12_r2", "2026mttd_p3"
    event_key: str | None = None    # "2026mttd"
    event_name: str | None = None   # "Madtown Throwdown" (TBA's name)
    year: int | None = None
    comp_level: str | None = None   # "qm" | "sf" | "f" | "p" (practice) | None
    set_number: int | None = None
    match_number: int | None = None
    replay: int = 1                 # 2 for "_r2"
    red: list[str] = field(default_factory=list)    # team numbers as shown by TBA without "frc": ["254", "9993"]
    blue: list[str] = field(default_factory=list)
    red_score: int | None = None    # filled once TBA posts the result
    blue_score: int | None = None


@dataclass
class Job:
    """One file for one destination."""
    id: str                         # unique and stable: "<match name>@<stream start>:<kind>:<destination>"
    destination: str                # "slack" | "youtube"
    kind: str                       # "clip" (the match), "score" (match + results screen), "test" (wizard)
    path: str                       # the MP4 to send
    match: Match
    state: dict = field(default_factory=dict)   # destination-owned resume state (e.g. a YouTube upload session URI)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Job":
        return Job(**{**d, "match": Match(**d["match"])})


class Destination(Protocol):
    name: str                       # "slack" | "youtube"

    def send(self, job: Job) -> dict:
        """Deliver `job` (one attempt; may resume from job.state, and may update job.state as it goes so a retry
        continues). Returns a result, e.g. {"url": ...}. Raises TransientError or PermanentError."""
        ...

"""Repository 导出。"""

from repository.base import BaseRepository
from repository.candidate import CandidateRepository, build_candidate_repository
from repository.email_thread import EmailThreadRepository, build_email_thread_repository
from repository.event import EventRepository, build_event_repository
from repository.message import MessageRepository, build_message_repository
from repository.position import PositionRepository, SupervisorRepository

__all__ = [
    "BaseRepository",
    "CandidateRepository",
    "EmailThreadRepository",
    "EventRepository",
    "MessageRepository",
    "PositionRepository",
    "SupervisorRepository",
    "build_candidate_repository",
    "build_email_thread_repository",
    "build_event_repository",
    "build_message_repository",
]

"""Shared message roles, independent of storage client dependencies."""
from enum import Enum


class MsgRole(Enum):
    USER = 'user'
    ASSISTANT = 'assistant'
    SYSTEM = 'system'

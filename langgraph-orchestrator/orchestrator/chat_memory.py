"""Bounded conversation memory shared by Chainlit and the orchestrator."""
from typing import Iterable, Mapping, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


MAX_CHAT_TURNS = 10


class ChatTurn(TypedDict):
    user: str
    assistant: str


def _clean_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_chat_history(
    history: Iterable[Mapping[str, object]] | None,
    limit: int = MAX_CHAT_TURNS,
) -> list[ChatTurn]:
    """Return only complete, normalized turns, capped to the newest entries."""
    if limit <= 0:
        return []

    turns: list[ChatTurn] = []
    for item in history or []:
        user = _clean_text(item.get("user"))
        assistant = _clean_text(item.get("assistant"))
        if user and assistant:
            turns.append({"user": user, "assistant": assistant})
    return turns[-limit:]


def append_chat_turn(
    history: Iterable[Mapping[str, object]] | None,
    user: str,
    assistant: str,
    limit: int = MAX_CHAT_TURNS,
) -> list[ChatTurn]:
    """Append one completed exchange and enforce the configured turn limit."""
    return normalize_chat_history(
        [*(history or []), {"user": user, "assistant": assistant}],
        limit=limit,
    )


def history_to_messages(
    history: Iterable[Mapping[str, object]] | None,
    limit: int = MAX_CHAT_TURNS,
) -> list[BaseMessage]:
    """Convert stored turns to ordered LangChain chat messages."""
    messages: list[BaseMessage] = []
    for turn in normalize_chat_history(history, limit=limit):
        messages.extend([
            HumanMessage(content=turn["user"]),
            AIMessage(content=turn["assistant"]),
        ])
    return messages


def reconstruct_chat_history(
    steps: Sequence[Mapping[str, object]] | None,
    excluded_user_messages: Iterable[str] = (),
    limit: int = MAX_CHAT_TURNS,
) -> list[ChatTurn]:
    """Rebuild completed root user/assistant exchanges from Chainlit steps."""
    records = list(steps or [])
    timestamps = [
        step.get("start") or step.get("createdAt") or step.get("created_at")
        for step in records
    ]
    if records and all(isinstance(value, str) and value for value in timestamps):
        records = [
            step for _, step in sorted(
                zip(timestamps, records), key=lambda pair: pair[0]
            )
        ]

    excluded = {_clean_text(value).lower() for value in excluded_user_messages}
    turns: list[ChatTurn] = []
    pending_user = ""
    pending_assistant = ""

    def finish_pending() -> None:
        if pending_user and pending_assistant:
            turns.append({"user": pending_user, "assistant": pending_assistant})

    for step in records:
        if step.get("parentId") or step.get("parent_id"):
            continue
        message_type = step.get("type")
        if message_type not in {"user_message", "assistant_message"}:
            continue
        content = _clean_text(step.get("output") or step.get("content"))
        if not content:
            continue

        if message_type == "user_message":
            finish_pending()
            pending_assistant = ""
            pending_user = "" if content.lower() in excluded else content
        elif pending_user:
            pending_assistant = content

    finish_pending()
    return normalize_chat_history(turns, limit=limit)
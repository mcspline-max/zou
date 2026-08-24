"""
Pydantic schemas for request body validation in the shared playlist
blueprint. Use them with `validate_request_body()` to surface clear
field-level errors and keep the routes aligned with the rest of the
codebase.
"""

from typing import Any, List, Optional
from uuid import UUID

from pydantic import Field, field_validator

from zou.app.utils.validation import BaseSchema


class CreateGuestSchema(BaseSchema):
    """
    Body for creating or reusing a guest on a shared playlist.
    """

    first_name: Optional[str] = Field(
        "Guest",
        description="First name of the guest reviewer",
    )
    last_name: Optional[str] = Field(
        "",
        description="Last name of the guest reviewer",
    )
    guest_id: Optional[UUID] = Field(
        None,
        description=(
            "Existing guest identifier — when provided the server "
            "looks the guest up instead of creating a new one"
        ),
    )

    @field_validator("guest_id", mode="before")
    @classmethod
    def coerce_empty_to_none(cls, v):
        if v == "":
            return None
        return v


class CreateGuestCommentSchema(BaseSchema):
    """
    Body for posting a comment as a guest.
    """

    guest_id: UUID = Field(..., description="Guest unique identifier")
    task_id: UUID = Field(
        ...,
        description="Task to comment on (must belong to the shared playlist)",
    )
    task_status_id: UUID = Field(
        ...,
        description=("Target task status. Must be flagged is_client_allowed."),
    )
    text: Optional[str] = Field(
        "",
        description="Comment body",
    )
    checklist: Optional[List[Any]] = Field(
        None,
        description="Optional checklist entries attached to the comment",
    )
    timecode: Optional[float] = Field(
        None, description="Timecode of the comment in a video, in seconds"
    )
    preview_file_id: Optional[UUID] = Field(
        None,
        description=(
            "Preview file the comment's timecode was captured against, so "
            "it only surfaces while that revision is the one being viewed"
        ),
    )
    annotation: Optional[dict] = Field(
        None,
        description=(
            "Drawing made at the comment's timecode, owned by this comment"
        ),
    )


class EditGuestCommentSchema(BaseSchema):
    """
    Body for editing a guest-owned comment.

    Only the fields the service consumes (``text``, ``checklist``,
    ``task_status_id``, ``timecode``) are accepted; ``guest_id``
    identifies the guest on whose behalf the edit runs.

    """

    guest_id: UUID = Field(..., description="Guest unique identifier")
    text: Optional[str] = Field(None, description="New comment body")
    checklist: Optional[List[Any]] = Field(
        None, description="New checklist entries"
    )
    timecode: Optional[float] = Field(
        None, description="New timecode of the comment in a video, in seconds"
    )
    task_status_id: Optional[UUID] = Field(
        None,
        description=(
            "New task status — must be flagged is_client_allowed, "
            "enforced server-side"
        ),
    )
    annotation: Optional[dict] = Field(
        None, description="Full replacement of the comment's own annotation"
    )


class ReplyGuestCommentSchema(BaseSchema):
    """
    Body for a guest replying to a comment visible in the shared playlist
    (their own or anyone else's — see reply_to_shared_comment).
    """

    guest_id: UUID = Field(
        ..., description="Guest unique identifier on whose behalf the reply is posted"
    )
    text: str = Field(..., description="Reply text content")


class GuestActionSchema(BaseSchema):
    """
    Lightweight schema for delete-style endpoints that only carry a
    `guest_id` (sent as JSON body or as a query string).
    """

    guest_id: UUID = Field(
        ...,
        description="Guest unique identifier on whose behalf the action runs",
    )


class UpdateGuestCommentAnnotationSchema(BaseSchema):
    """
    Body for the shared-playlist, comment-scoped annotation diff
    endpoint: applies to one guest-owned comment's own annotation while
    its author is still drawing.
    """

    guest_id: UUID = Field(..., description="Guest unique identifier")
    additions: Optional[List[Any]] = Field(
        None,
        description="Objects to add",
    )
    updates: Optional[List[Any]] = Field(
        None,
        description="Objects to update in place, matched by id",
    )
    deletions: Optional[List[Any]] = Field(
        None,
        description="Object identifiers to remove",
    )

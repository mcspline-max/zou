import datetime
import uuid

from sqlalchemy import func

from zou.app.utils import auth, events

from zou.app.models.entity import Entity
from zou.app.models.entity_type import EntityType
from zou.app.models.person import Person
from zou.app.models.playlist import Playlist
from zou.app.models.playlist_share_link import PlaylistShareLink
from zou.app.models.task import Task
from zou.app.models.task_status import TaskStatus
from zou.app.models.task_type import TaskType
from zou.app.services import (
    files_service,
    persons_service,
    playlists_service,
    tasks_service,
    projects_service,
)
from zou.app.services.exception import (
    PlaylistShareLinkNotFoundException,
    PreviewFileNotFoundException,
    WrongParameterException,
)


def _get_expiration_datetime(expiration_date):
    """
    Turn the expiration date of a share link into the instant it stops
    working. A day names the whole day: a manager who sets the link to
    expire on the 10th expects it to work on the 10th, so a date-only
    value lands on its last second rather than its first. A full
    timestamp is honoured as given.
    """
    if not expiration_date:
        return None
    try:
        return datetime.datetime.combine(
            datetime.date.fromisoformat(expiration_date), datetime.time.max
        )
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(expiration_date)
    except ValueError:
        raise WrongParameterException(
            f"{expiration_date} is not a valid ISO 8601 date"
        )


def create_share_link(
    playlist_id,
    person_id,
    expiration_date=None,
    can_comment=True,
    show_revision_selector=False,
    password=None,
):
    """
    Generate a share link for a playlist. Only managers and above
    should call this (enforced at the resource level).
    """
    playlists_service.get_playlist(playlist_id)
    token = str(uuid.uuid4())
    # Hash the password at rest so a DB leak (or a manager fetching the
    # share link list) never exposes the cleartext credential. An empty
    # or missing password is stored as NULL — the validate path then
    # treats the link as unprotected. encrypt_password returns bytes;
    # decode to str so it lands in the String column as a real hash
    # rather than the literal bytes repr.
    password_hash = (
        auth.encrypt_password(password).decode("utf-8") if password else None
    )
    share_link = PlaylistShareLink.create(
        token=token,
        playlist_id=playlist_id,
        created_by=person_id,
        expiration_date=_get_expiration_datetime(expiration_date),
        can_comment=can_comment,
        show_revision_selector=show_revision_selector,
        password=password_hash,
    )
    return share_link.serialize()


def get_share_links_for_playlist(playlist_id):
    """
    Return all active share links for a playlist.
    """
    playlists_service.get_playlist(playlist_id)
    links = PlaylistShareLink.get_all_by(
        playlist_id=playlist_id, is_active=True
    )
    return [link.serialize() for link in links]


def revoke_share_link(token):
    """
    Deactivate a share link without deleting it.
    """
    share_link = get_share_link_by_token_raw(token)
    share_link.update({"is_active": False})
    return share_link.serialize()


def update_share_link(token, can_comment=None, show_revision_selector=None):
    """
    Update the settings of an existing share link. Only the fields
    explicitly passed (not None) are changed — unlike creation, editing a
    live link shouldn't require restating every other setting.
    """
    share_link = get_share_link_by_token_raw(token)
    data = {}
    if can_comment is not None:
        data["can_comment"] = can_comment
    if show_revision_selector is not None:
        data["show_revision_selector"] = show_revision_selector
    if data:
        share_link.update(data)
    return share_link.serialize()


def get_share_link_by_token_raw(token):
    """
    Return the raw ORM share link for a token.
    Raises PlaylistShareLinkNotFoundException if not found.
    """
    share_link = PlaylistShareLink.get_by(token=token)
    if share_link is None:
        raise PlaylistShareLinkNotFoundException
    return share_link


def validate_share_token(token, password=None):
    """
    Validate that a share token is active and not expired.
    Returns the serialized share link on success.
    Raises appropriate exceptions on failure.
    """
    share_link = get_share_link_by_token_raw(token)
    if not share_link.is_active:
        raise PlaylistShareLinkNotFoundException

    if share_link.expiration_date is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        expiration = share_link.expiration_date
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=datetime.timezone.utc)
        if now > expiration:
            raise PlaylistShareLinkNotFoundException

    if share_link.password is not None and share_link.password != "":
        # Constant-time bcrypt verify; never a plain `!=`.
        if not password:
            raise PlaylistShareLinkNotFoundException
        try:
            if not auth.check_password(share_link.password, password):
                raise PlaylistShareLinkNotFoundException
        except (ValueError, TypeError):
            raise PlaylistShareLinkNotFoundException

    return share_link.serialize()


def get_shared_playlist(token):
    """
    Return the playlist data accessible via a share token.

    Validates the token first. Shots are returned with the same
    preview-file enrichment used by the public GET endpoint
    (``preview_file_task_id``, ``preview_file_previews``…), because the
    raw ``playlist.shots`` JSON only stores ``entity_id`` /
    ``preview_file_id`` for shots added via the playlist builder — guards
    that compare against task or sub-revision ids would otherwise
    always fail for those playlists.
    """
    share_link = validate_share_token(token)
    return playlists_service.get_playlist_with_preview_file_revisions(
        share_link["playlist_id"]
    )


def is_preview_file_in_shared_playlist(token, preview_file_id):
    """
    Membership check used by every shared-playlist file-serving
    endpoint (thumbnail, original, movie, download…).

    A shared playlist is read-only for the viewer, so the only previews
    legitimately served through the share link are those tied to the
    positioned revision of each shot. We accept either the exact
    preview_file_id stored on a shot, or another preview file that
    shares its (task, revision) with the positioned one — a single
    revision may carry several previews (different positions).

    The same entity may be listed several times in a playlist, each shot
    positioned on a different task type / preview file (e.g. reviewing the
    texturing, posing and expression previews of a single asset). We
    therefore gather *every* preview positioned for the requested entity
    instead of collapsing the shots into one entity -> preview_file_id
    mapping, which kept only the last shot and 403'd the previews
    positioned on the others.
    """
    share_link = validate_share_token(token)
    playlist = Playlist.get(share_link["playlist_id"])
    if playlist is None or not playlist.shots:
        return False
    preview_file = files_service.get_preview_file(preview_file_id)
    task = tasks_service.get_task(preview_file["task_id"])
    entity_id = str(task["entity_id"])

    positioned_ids = {
        str(shot["preview_file_id"])
        for shot in playlist.shots
        if str(shot.get("entity_id") or shot.get("id")) == entity_id
        and shot.get("preview_file_id")
    }
    if not positioned_ids:
        return False
    if str(preview_file_id) in positioned_ids:
        return True

    # Accept the other positions of a positioned revision: a single revision
    # may carry several preview files (same task, same revision, different
    # position). A different revision — or the same revision number on another
    # task type of the entity — stays rejected, unless the link explicitly
    # allows switching revisions, in which case any revision of the same
    # task as a positioned shot is fair game too.
    show_revision_selector = share_link.get("show_revision_selector", False)
    for positioned_id in positioned_ids:
        try:
            main_preview_file = files_service.get_preview_file(positioned_id)
        except PreviewFileNotFoundException:
            # A deleted preview can stay referenced in playlist.shots
            # (deletion does not scrub the shots column). Skip the dangling
            # id instead of letting it mask a valid sibling/revision match.
            continue
        if str(main_preview_file["task_id"]) != str(preview_file["task_id"]):
            continue
        if main_preview_file["revision"] == preview_file["revision"]:
            return True
        if show_revision_selector:
            return True
    return False


class GuestCommentForbidden(Exception):
    pass


class GuestCommentNotFound(Exception):
    pass


def _load_guest_comment(comment_id, guest_id, token):
    """
    Fetch a comment by id and ensure (a) it was authored by the given guest
    and (b) it lives on a task that is part of the playlist exposed by the
    share token. Without (b), a guest who has commented on tasks across
    several playlists could mutate any of those comments through any
    single share link they hold.

    Raises :class:`GuestCommentForbidden` or :class:`GuestCommentNotFound`.
    """
    if not guest_id:
        raise GuestCommentForbidden
    # Ensure the guest exists AND was created from this very share link;
    # rejects replayed guest UUIDs leaked from another link.
    share_link = validate_share_token(token)
    try:
        get_guest_for_share_link(guest_id, share_link)
    except Exception:
        raise GuestCommentForbidden
    try:
        comment = tasks_service.get_comment(comment_id)
    except Exception:
        raise GuestCommentNotFound
    if str(comment.get("person_id")) != str(guest_id):
        raise GuestCommentForbidden
    task_id = comment.get("object_id")
    if task_id is None:
        raise GuestCommentForbidden
    # Use the enriched playlist so preview_file_task_id is populated for
    # shots added via the playlist builder, which only stores entity_id /
    # preview_file_id at rest.
    playlist = playlists_service.get_playlist_with_preview_file_revisions(
        share_link["playlist_id"]
    )
    playlist_task_ids = {
        str(shot.get("preview_file_task_id"))
        for shot in playlist.get("shots", []) or []
        if shot.get("preview_file_task_id")
    }
    if str(task_id) not in playlist_task_ids:
        raise GuestCommentForbidden
    return comment


def update_guest_comment(comment_id, guest_id, data, token):
    """
    Update a comment authored by a guest. Accepts ``text``, ``checklist``,
    ``task_status_id`` and ``timecode`` in ``data``. Triggers the same
    post-update side effects as the regular CRUD path (reset mentions,
    cache, events, task status reset when needed).

    The ``token`` is used to scope the comment to the share link's playlist,
    and also to reject ``task_status_id`` values that are not flagged as
    client-allowed (a guest must not be able to set a manager-only status).
    """
    from zou.app.models.comment import Comment
    from zou.app.services import comments_service, notifications_service

    instance = _load_guest_comment(comment_id, guest_id, token)

    new_status_id = data.get("task_status_id")
    status_changed = bool(
        new_status_id and instance.get("task_status_id") != new_status_id
    )
    if status_changed:
        try:
            new_status = tasks_service.get_task_status(new_status_id)
        except Exception:
            raise GuestCommentForbidden
        if not new_status.get("is_client_allowed", False):
            raise GuestCommentForbidden
    previous_status_id = instance.get("task_status_id")

    comment_row = Comment.get(comment_id)
    if "text" in data:
        comment_row.text = data["text"] or ""
    if "checklist" in data:
        comment_row.checklist = data["checklist"] or []
    if "timecode" in data:
        comment_row.timecode = data["timecode"]
    if "annotation" in data:
        comment_row.annotation = data["annotation"]
    if new_status_id:
        comment_row.task_status_id = new_status_id
    comment_row.editor_id = guest_id
    comment_row.save()

    # reset_mentions walks the mentions table; feed it the relations-loaded
    # dict so it has the `mentions` / `department_mentions` keys it expects.
    tasks_service.clear_comment_cache(comment_id)
    updated = tasks_service.get_comment(comment_id, relations=True)
    comments_service.reset_mentions(updated)

    task_id = updated["object_id"]
    task = tasks_service.get_task(task_id)
    if status_changed:
        tasks_service.reset_task_data(task_id)
        events.emit(
            "task:status-changed",
            {
                "task_id": task_id,
                "new_task_status_id": new_status_id,
                "previous_task_status_id": previous_status_id,
                "person_id": guest_id,
            },
            project_id=task["project_id"],
        )
    tasks_service.clear_comment_cache(comment_id)
    try:
        notifications_service.reset_notifications_for_mentions(updated)
    except KeyError:
        # Some serialized dicts lack the `mentions` key; guest comments
        # never carry mentions anyway, so ignore.
        pass
    events.emit(
        "comment:update",
        {"comment_id": updated["id"], "task_id": task_id},
        project_id=task["project_id"],
    )
    return _serialize_enriched_comment(comment_id)


def update_guest_comment_annotation(
    comment_id, guest_id, token, additions=None, updates=None, deletions=None
):
    """
    Apply an additions/updates/deletions diff to a guest-owned comment's
    own annotation, while its author is still drawing. Reuses
    `_load_guest_comment` for the same ownership + playlist-scope check
    `update_guest_comment`/`delete_guest_comment` already enforce, then
    delegates the actual diff to `preview_files_service` (Redis-locked,
    author-checked again there).
    """
    from zou.app.services import preview_files_service

    _load_guest_comment(comment_id, guest_id, token)
    result = preview_files_service.apply_comment_annotation_diff(
        comment_id,
        guest_id,
        additions=additions,
        updates=updates,
        deletions=deletions,
    )
    tasks_service.clear_comment_cache(comment_id)
    return result


def delete_guest_comment(comment_id, guest_id, token):
    """
    Delete a comment authored by a guest. Triggers the same side effects as
    the regular CRUD delete: removal via ``deletion_service``, task data
    reset and task status event if the status changed.

    Scoped to the share link via ``token`` so a guest cannot delete a
    comment they authored on a task that is not part of this playlist.
    """
    from zou.app.services import deletion_service

    instance = _load_guest_comment(comment_id, guest_id, token)

    task_id = instance["object_id"]
    task_before = tasks_service.get_task(task_id)
    previous_status_id = task_before["task_status_id"]

    deletion_service.remove_comment(comment_id)
    tasks_service.reset_task_data(task_id)
    tasks_service.clear_comment_cache(comment_id)

    task_after = tasks_service.get_task(task_id)
    new_status_id = task_after["task_status_id"]
    if previous_status_id != new_status_id:
        events.emit(
            "task:status-changed",
            {
                "task_id": task_id,
                "new_task_status_id": new_status_id,
                "previous_task_status_id": previous_status_id,
                "person_id": guest_id,
            },
            project_id=task_after["project_id"],
        )


def _load_visible_shared_comment(comment_id, guest_id, token):
    """
    Fetch a comment by id and ensure (a) guest_id is a guest bound to this
    share link, (b) the comment lives on a task that's part of the
    playlist the token exposes, and (c) the comment is visible in the
    shared view (get_shared_task_comments' own rule: for_client, or
    authored by a guest).

    Shared by every guest action that may target ANY visible comment —
    reply/acknowledge, and (unlike update_guest_comment/delete_guest_comment,
    which are ownership checks) not just the guest's own — mirroring how a
    studio member can already reply to/acknowledge a guest's comment from
    the studio side.

    Raises GuestCommentForbidden or GuestCommentNotFound.
    """
    if not guest_id:
        raise GuestCommentForbidden
    share_link = validate_share_token(token)
    try:
        get_guest_for_share_link(guest_id, share_link)
    except Exception:
        raise GuestCommentForbidden

    try:
        comment = tasks_service.get_comment(comment_id)
    except Exception:
        raise GuestCommentNotFound

    task_id = comment.get("object_id")
    if task_id is None:
        raise GuestCommentNotFound
    # Use the enriched playlist so preview_file_task_id is populated for
    # shots added via the playlist builder, which only stores entity_id /
    # preview_file_id at rest (same reasoning as _load_guest_comment).
    playlist = playlists_service.get_playlist_with_preview_file_revisions(
        share_link["playlist_id"]
    )
    playlist_task_ids = {
        str(shot.get("preview_file_task_id"))
        for shot in playlist.get("shots", []) or []
        if shot.get("preview_file_task_id")
    }
    if str(task_id) not in playlist_task_ids:
        raise GuestCommentNotFound

    author = Person.get(comment.get("person_id"))
    author_is_guest = bool(author and author.is_guest)
    if not (comment.get("for_client") or author_is_guest):
        raise GuestCommentNotFound

    return comment


def reply_to_shared_comment(comment_id, guest_id, text, token):
    """
    Add a reply to a comment visible in this shared playlist, as the given
    guest.
    """
    from zou.app.services import comments_service

    _load_visible_shared_comment(comment_id, guest_id, token)
    return comments_service.reply_comment(comment_id, text, person_id=guest_id)


def acknowledge_shared_comment(comment_id, guest_id, token):
    """
    Toggle the given guest's acknowledgement ("like") on a comment visible
    in this shared playlist. A guest request carries no JWT, so
    comments_service.acknowledge_comment is called with an explicit
    person_id instead of resolving the current user.
    """
    from zou.app.services import comments_service

    _load_visible_shared_comment(comment_id, guest_id, token)
    return comments_service.acknowledge_comment(comment_id, person_id=guest_id)


class ReplyForbidden(Exception):
    pass


class ReplyNotFound(Exception):
    pass


def _find_reply(comment, reply_id):
    for reply in comment.get("replies") or []:
        if str(reply.get("id")) == str(reply_id):
            return reply
    return None


def edit_shared_comment_reply(comment_id, reply_id, guest_id, text, token):
    """
    Edit a reply on a comment visible in this shared playlist. Unlike
    reply_to_shared_comment (any visible comment), this IS an ownership
    check: only the guest who wrote the reply may edit it.
    """
    from zou.app.services import comments_service

    comment = _load_visible_shared_comment(comment_id, guest_id, token)
    reply = _find_reply(comment, reply_id)
    if reply is None:
        raise ReplyNotFound
    if str(reply.get("person_id")) != str(guest_id):
        raise ReplyForbidden
    return comments_service.edit_reply(comment_id, reply_id, text)


def delete_shared_comment_reply(comment_id, reply_id, guest_id, token):
    """
    Delete a reply from a comment visible in this shared playlist. Same
    ownership check as edit_shared_comment_reply: only the guest who wrote
    the reply may delete it.
    """
    from zou.app.services import comments_service

    comment = _load_visible_shared_comment(comment_id, guest_id, token)
    reply = _find_reply(comment, reply_id)
    if reply is None:
        raise ReplyNotFound
    if str(reply.get("person_id")) != str(guest_id):
        raise ReplyForbidden
    comments_service.delete_reply(comment_id, reply_id)


def _serialize_enriched_comment(comment_id):
    """
    Return a comment dict with `attachment_files` expanded to full objects
    (same shape as `_run_task_comments_query`'s output), so the shared client
    can render filenames/sizes without extra lookups.
    """
    from zou.app.models.attachment_file import AttachmentFile

    comment = tasks_service.get_comment(comment_id, relations=True)
    ids = comment.get("attachment_files") or []
    if ids and all(isinstance(item, str) for item in ids):
        attachments = AttachmentFile.query.filter(
            AttachmentFile.id.in_(ids)
        ).all()
        comment["attachment_files"] = [af.present() for af in attachments]
    _embed_shared_reply_authors([comment])
    return comment


def add_guest_comment_attachments(comment_id, guest_id, files, token):
    """
    Attach uploaded files to a comment authored by the given guest, scoped
    to the share link's playlist via ``token``.
    Returns the updated comment dict (with relations).
    """
    from zou.app.services import comments_service

    comment = _load_guest_comment(comment_id, guest_id, token)
    comments_service.add_attachments_to_comment(comment, files)
    return _serialize_enriched_comment(comment_id)


def download_shared_attachment(token, attachment_id, file_name):
    """
    Serve an attachment file linked to a comment that is visible to this
    share link (guest-posted or for_client=True on a task that's part of the
    playlist). Raises ``GuestCommentNotFound`` if the attachment is not
    served by this link.
    """
    from flask import send_file as flask_send_file
    from zou.app.services import comments_service

    attachment = comments_service.get_attachment_file(attachment_id)
    comment_id = attachment.get("comment_id")
    if not comment_id:
        raise GuestCommentNotFound

    comment = tasks_service.get_comment(comment_id)
    task_id = comment.get("object_id")
    if not task_id:
        raise GuestCommentNotFound

    # The comment must belong to a task in the playlist that this token
    # shares, and must be visible (guest or for_client).
    playlist = get_shared_playlist(token)
    task_ids = {
        str(shot.get("preview_file_task_id"))
        for shot in playlist.get("shots", [])
        if shot.get("preview_file_task_id")
    }
    if str(task_id) not in task_ids:
        raise GuestCommentNotFound

    author_is_guest = False
    if comment.get("person_id"):
        person = persons_service.get_person(comment["person_id"])
        author_is_guest = bool(person.get("is_guest"))
    if not (comment.get("for_client") or author_is_guest):
        raise GuestCommentNotFound

    file_path = comments_service.get_attachment_file_path(attachment)
    return flask_send_file(
        file_path,
        conditional=True,
        mimetype=attachment["mimetype"],
        # Serve safe raster images inline; force download for everything else.
        # This path is unauthenticated (share link) and the mimetype is
        # attacker-controlled, so serving e.g. HTML/SVG inline would allow a
        # stored XSS in Kitsu's origin.
        as_attachment=not comments_service.is_inline_safe_mimetype(
            attachment["mimetype"]
        ),
        download_name=attachment["name"],
    )


def remove_guest_comment_attachment(
    comment_id, guest_id, attachment_id, token
):
    """
    Remove a single attachment from a comment authored by the given guest,
    scoped to the share link's playlist via ``token``.
    """
    from zou.app.models.attachment_file import AttachmentFile
    from zou.app.services import deletion_service

    _load_guest_comment(comment_id, guest_id, token)
    attachment = AttachmentFile.get(attachment_id)
    if attachment is None or str(attachment.comment_id) != str(comment_id):
        raise GuestCommentNotFound
    deletion_service.remove_attachment_file(attachment)


def _embed_shared_reply_authors(comments):
    """
    Attach a full author to each reply on the given comments, in place.

    Unlike tasks_service.embed_reply_authors (used by the authenticated
    client view, which hides non-client reply authors to keep studio
    identities private on comments a client wasn't shown), a reply here
    only ever exists on a comment that's already visible to the guest —
    the same way a studio member's for_client top-level comment already
    shows their name — so every reply author is embedded regardless of
    role. Shared by every path that hands a comment back to a guest:
    the task-comments list and a single freshly-edited comment alike.
    """
    reply_person_ids = {
        reply.get("person_id")
        for comment in comments
        for reply in (comment.get("replies") or [])
        if reply.get("person_id")
    }
    if not reply_person_ids:
        return
    guest_ids = {
        str(person_id)
        for (person_id,) in Person.query.filter_by(is_guest=True)
        .with_entities(Person.id)
        .all()
    }
    persons_map = persons_service.get_short_persons_map(list(reply_person_ids))
    for comment in comments:
        for reply in comment.get("replies") or []:
            author = persons_map.get(reply.get("person_id"))
            if author:
                author = {
                    **author,
                    "is_guest": str(reply.get("person_id")) in guest_ids,
                }
            reply["person"] = author


def get_shared_task_comments(task_id):
    """
    Return comments visible in the shared context for a task: those flagged
    `for_client=True` plus those posted by a guest. Bypasses
    tasks_service.get_comments which requires a JWT-authenticated current
    user.
    """
    from zou.app.services.tasks_service import (
        _build_ack_map_for_comments,
        _build_attachment_map_for_comments,
        _build_department_mention_map_for_comments,
        _build_mention_map_for_comments,
        _prepare_query,
        _run_task_comments_query,
    )

    query = _prepare_query(task_id, is_client=True, is_manager=False)
    comments, comment_ids = _run_task_comments_query(query)

    # _run_task_comments_query only builds the comment rows themselves;
    # the linked records come from separate grouped queries, exactly as
    # tasks_service.get_comments does after calling it. Without this the
    # shared client never sees an attachment posted from the studio.
    # Previews stay out on purpose: the shared player already shows the
    # revision being reviewed, and the guest has no business seeing the
    # studio's other revisions listed under each comment.
    if comments:
        ack_map = _build_ack_map_for_comments(comment_ids)
        mention_map = _build_mention_map_for_comments(comment_ids)
        department_mention_map = _build_department_mention_map_for_comments(
            comment_ids
        )
        attachment_file_map = _build_attachment_map_for_comments(comment_ids)
        for comment in comments:
            comment["acknowledgements"] = ack_map.get(comment["id"], [])
            comment["mentions"] = mention_map.get(comment["id"], [])
            comment["department_mentions"] = department_mention_map.get(
                comment["id"], []
            )
            comment["attachment_files"] = attachment_file_map.get(
                comment["id"], []
            )

    guest_ids = {
        str(person_id)
        for (person_id,) in Person.query.filter_by(is_guest=True)
        .with_entities(Person.id)
        .all()
    }
    visible = []
    for comment in comments:
        author_id = str(comment.get("person_id", ""))
        is_guest_author = author_id in guest_ids
        if not (comment.get("for_client") or is_guest_author):
            continue
        if comment.get("person"):
            comment["person"]["is_guest"] = is_guest_author
        visible.append(comment)

    _embed_shared_reply_authors(visible)

    return visible


# Entity types for which "parent" is a parent record (shot/seq/episode/…).
# For other types, we show the entity type name as the logical parent
# (e.g. assets).
_SHOT_LIKE_ENTITY_TYPE_NAMES = frozenset(
    ("Shot", "Sequence", "Episode", "Edit", "Concept")
)


def _enrich_shared_playlist_project_line(playlist_dict):
    """
    Inline the project name, fps and episode name on the playlist: a shared
    viewer has no authenticated access to the project or entity stores.
    """
    project_id = playlist_dict.get("project_id")
    if project_id:
        project = projects_service.get_project(str(project_id))
        playlist_dict["project_fps"] = project.get("fps")
        playlist_dict["project_name"] = project.get("name")

    episode_id = playlist_dict.get("episode_id")
    playlist_dict["episode_name"] = None
    if episode_id:
        episode = Entity.query.get(episode_id)
        if episode is not None:
            playlist_dict["episode_name"] = episode.name


def _load_task_styling_by_task_id(task_ids):
    """
    Return (task_type_by_task_id, task_status_by_task_id) dicts. The
    latter carries both the status id (needed to post a comment/
    annotation without silently changing the task's status) and its
    color (the only thing a shared viewer could show before).
    """
    if not task_ids:
        return {}, {}

    rows = (
        Task.query.join(TaskType, TaskType.id == Task.task_type_id)
        .join(TaskStatus, TaskStatus.id == Task.task_status_id)
        .filter(Task.id.in_(task_ids))
        .add_columns(
            Task.id,
            TaskType.id,
            TaskType.name,
            TaskType.color,
            TaskType.for_entity,
            TaskStatus.id,
            TaskStatus.color,
        )
        .all()
    )
    task_type_by_task_id = {}
    task_status_by_task_id = {}
    for (
        _,
        task_id,
        task_type_id,
        task_type_name,
        task_type_color,
        task_type_for_entity,
        task_status_id,
        task_status_color,
    ) in rows:
        tid = str(task_id)
        task_type_by_task_id[tid] = {
            "id": str(task_type_id),
            "name": task_type_name,
            "color": task_type_color,
            "for_entity": task_type_for_entity,
        }
        task_status_by_task_id[tid] = {
            "id": str(task_status_id),
            "color": task_status_color,
        }
    return task_type_by_task_id, task_status_by_task_id


def _parent_name_for_shot_entry(entity, entity_type_name, parent_map):
    """
    Return what is shown as the parent of a playlist entry: the parent
    record name for shot-like entities, the entity type name otherwise.
    """
    if entity_type_name in _SHOT_LIKE_ENTITY_TYPE_NAMES:
        if entity.parent_id is None:
            return ""
        return parent_map.get(str(entity.parent_id), "")
    return entity_type_name


def _apply_task_styling_to_shot(
    shot, task_id, task_type_by_task_id, task_status_by_task_id
):
    """
    Inline the task type and current status a shared viewer cannot look
    up on their own — the color for display, and the id so a guest
    comment/annotation can be posted against the task's actual current
    status without silently changing it.
    """
    tid = str(task_id)
    task_type = task_type_by_task_id.get(tid)
    if task_type:
        shot["preview_file_task_type"] = task_type
        shot["preview_file_task_type_name"] = task_type["name"]
    task_status = task_status_by_task_id.get(tid)
    if task_status:
        shot["task_status_id"] = task_status["id"]
        shot["task_status_color"] = task_status["color"]


def enrich_shots_with_entity_info(playlist_dict):
    """
    Augment each shot entry in the playlist with `name` and `parent_name`
    (sequence/episode/asset_type name). The stored `playlist.shots` only
    keeps preview/entity references — in the shared context, consumers
    have no auth'd access to entity/asset/shot stores, so names must be
    inlined here.
    """
    _enrich_shared_playlist_project_line(playlist_dict)

    shots = playlist_dict.get("shots") or []
    entity_ids = [shot["id"] for shot in shots if shot.get("id")]
    if not entity_ids:
        return playlist_dict

    entities = Entity.query.filter(Entity.id.in_(entity_ids)).all()
    entity_map = {str(e.id): e for e in entities}

    parent_ids = {str(e.parent_id) for e in entities if e.parent_id}
    parent_map = {}
    if parent_ids:
        parent_map = {
            str(p.id): p.name
            for p in Entity.query.filter(Entity.id.in_(parent_ids)).all()
        }

    type_ids = {str(e.entity_type_id) for e in entities if e.entity_type_id}
    type_map = {}
    if type_ids:
        type_map = {
            str(t.id): t.name
            for t in EntityType.query.filter(EntityType.id.in_(type_ids)).all()
        }

    task_ids = {
        s["preview_file_task_id"]
        for s in shots
        if s.get("preview_file_task_id")
    }
    task_type_by_task_id, task_status_by_task_id = (
        _load_task_styling_by_task_id(task_ids)
    )

    for shot in shots:
        entity = entity_map.get(str(shot.get("id")))
        if entity is None:
            continue
        shot["name"] = entity.name
        entity_type_name = type_map.get(str(entity.entity_type_id), "")
        shot["parent_name"] = _parent_name_for_shot_entry(
            entity, entity_type_name, parent_map
        )
        task_id = shot.get("preview_file_task_id")
        if not task_id:
            continue
        _apply_task_styling_to_shot(
            shot,
            task_id,
            task_type_by_task_id,
            task_status_by_task_id,
        )
    return playlist_dict


def create_guest(token, first_name, last_name=""):
    """
    Return or create a guest Person scoped to the share link of ``token``.

    Each guest is bound to the share link that created it (via
    ``Person.data['share_link_id']``). Reuse by first/last name only matches
    guests created from the same share link, so a reviewer named "John
    Smith" who has commented through link A cannot be impersonated by an
    attacker who later creates a guest with the same name through link B.
    The guest always has ``is_guest=True`` and ``role=client``.

    The match is case-insensitive: a reviewer who logs out and back in
    doesn't necessarily retype their name with the exact same
    capitalization (autocapitalize, a different device, plain habit), and
    a strict match would silently mint a second guest identity, orphaning
    every comment they'd already posted from the "same" name.
    """
    share_link = validate_share_token(token)
    share_link_id = str(share_link["id"])
    first_name = (first_name or "Guest").strip()
    last_name = (last_name or "").strip()

    existing = (
        Person.query.filter_by(is_guest=True)
        .filter(func.lower(Person.first_name) == first_name.lower())
        .filter(func.lower(Person.last_name) == last_name.lower())
        .filter(Person.data["share_link_id"].astext == share_link_id)
        .first()
    )
    if existing is not None:
        return existing.serialize()

    guest = Person.create(
        first_name=first_name,
        last_name=last_name,
        email=f"guest-{uuid.uuid4().hex[:8]}@guest.kitsu",
        role="client",
        is_guest=True,
        data={"share_link_id": share_link_id},
    )
    persons_service.clear_person_cache()
    events.emit("person:new", {"person_id": str(guest.id)})
    return guest.serialize()


def get_guest(guest_id):
    """
    Retrieve a guest person. Raises if not found or not a guest.
    """
    person = persons_service.get_person(guest_id)
    if not person.get("is_guest", False):
        raise PlaylistShareLinkNotFoundException
    return person


def get_guest_for_share_link(guest_id, share_link):
    """
    Retrieve a guest person, but only if it was created from the supplied
    share link. Used everywhere a ``guest_id`` is read from a request body
    so that an attacker holding one share token cannot replay another
    reviewer's guest UUID (leaked from a different link) to impersonate
    them. Raises :class:`PlaylistShareLinkNotFoundException` otherwise.
    """
    person = get_guest(guest_id)
    if person.get("data", {}).get("share_link_id") != str(share_link["id"]):
        raise PlaylistShareLinkNotFoundException
    return person


def get_shared_playlist_context(token):
    """
    Return the minimal project context needed to display a shared
    playlist: task types, task statuses, and entity names referenced
    by the playlist.
    """
    share_link = validate_share_token(token)
    playlist = playlists_service.get_playlist(share_link["playlist_id"])
    project_id = playlist["project_id"]
    project = projects_service.get_project(project_id)
    organisation = persons_service.get_organisation()

    task_types = projects_service.get_project_task_types(project_id)
    task_statuses = projects_service.get_project_task_statuses(project_id)

    # Collect entity names from playlist shots
    entity_names = {}
    for shot_entry in playlist.get("shots", []):
        entity_id = shot_entry.get("entity_id")
        if entity_id and entity_id not in entity_names:
            try:
                from zou.app.services import entities_service

                entity = entities_service.get_entity(entity_id)
                entity_names[entity_id] = {
                    "id": entity_id,
                    "name": entity.get("name", ""),
                    "preview_file_id": entity.get("preview_file_id"),
                }
            except Exception:
                pass

    return {
        "project": {
            "id": project["id"],
            "name": project["name"],
            "fps": project.get("fps"),
            "ratio": project.get("ratio"),
            "resolution": project.get("resolution"),
            "team": [],  # required by Kitsu
        },
        "organisation": {
            "name": organisation["name"],
            "has_avatar": organisation["has_avatar"],
        },
        # Task types are sent without `department_id` on purpose: the shared
        # client never populates the department map, and several widgets
        # (EditCommentModal, comment mentions) crash when they try to look
        # up an unknown department.
        "task_types": [
            {
                "id": tt["id"],
                "name": tt["name"],
                "color": tt["color"],
                "for_entity": tt.get("for_entity"),
            }
            for tt in task_types
        ],
        "task_statuses": [
            {
                "id": ts["id"],
                "name": ts["name"],
                "short_name": ts["short_name"],
                "color": ts["color"],
                "is_client_allowed": ts.get("is_client_allowed", False),
                "is_default": ts.get("is_default", False),
                "for_concept": ts.get("for_concept", False),
            }
            for ts in task_statuses
        ],
        "entities": list(entity_names.values()),
    }


def send_share_invitations(
    playlist_id,
    token,
    author_id,
    emails=None,
    person_ids=None,
    message=None,
):
    """
    Email a shared-playlist review invitation to one or more recipients
    (free-form emails and/or existing Persons looked up by id). Returns
    the deduplicated, normalized list of email addresses an invitation
    was actually dispatched to. Fire-and-forget — no DB record is kept.

    The token is validated against the playlist so a manager who knows
    *some* token cannot use a different playlist URL to invite people
    to a link they don't own.
    """
    from zou.app import config
    from zou.app.services import emails_service

    share_link = get_share_link_by_token_raw(token)
    if str(share_link.playlist_id) != str(playlist_id):
        raise PlaylistShareLinkNotFoundException
    if not share_link.is_active:
        raise PlaylistShareLinkNotFoundException

    playlist = playlists_service.get_playlist(playlist_id)
    project = projects_service.get_project(playlist["project_id"])
    author = persons_service.get_person(author_id)

    # Skip DNS deliverability checks: invitations should go out even when
    # the inviter typed a domain that hasn't published an MX record (and
    # doing live DNS in a request handler is brittle).
    recipients = {}
    for raw_email in emails or []:
        normalized = auth.validate_email(raw_email, check_deliverability=False)
        recipients.setdefault(
            normalized.lower(),
            {"email": normalized, "locale": None},
        )
    for person_id in person_ids or []:
        try:
            person = persons_service.get_person(str(person_id))
        except Exception:
            continue
        person_email = person.get("email")
        if not person_email:
            continue
        normalized = auth.validate_email(
            person_email, check_deliverability=False
        )
        recipients[normalized.lower()] = {
            "email": normalized,
            "locale": person.get("locale"),
        }

    share_url = (
        f"{config.DOMAIN_PROTOCOL}://{config.DOMAIN_NAME}"
        f"/playlists/shared/{token}"
    )

    sent = []
    for entry in recipients.values():
        emails_service.send_share_invitation(
            entry["email"],
            author,
            playlist,
            project,
            share_url,
            message=message,
            locale=entry["locale"],
        )
        sent.append(entry["email"])
    return sent

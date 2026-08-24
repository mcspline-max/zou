from flask import current_app, g, request
from flask_fs.errors import FileNotFound
from flask.views import MethodView

from zou.app.blueprints.previews.resources import (
    ALLOWED_FILE_EXTENSION,
    ALLOWED_PICTURE_EXTENSION,
    send_movie_file,
    send_picture_file,
    send_standard_file,
)
from zou.app.blueprints.shared.decorators import (
    require_valid_playlist_share_link,
)
from zou.app.models.preview_file import PreviewFile
from zou.app.blueprints.shared.schemas import (
    CreateGuestCommentSchema,
    CreateGuestSchema,
    EditGuestCommentSchema,
    GuestActionSchema,
    ReplyGuestCommentSchema,
    UpdateGuestCommentAnnotationSchema,
)
from zou.app.services import (
    comments_service,
    files_service,
    persons_service,
    playlist_sharing_service,
    playlists_service,
    preview_files_service,
    tasks_service,
)
from zou.app.services.exception import (
    AnnotationLockTimeoutException,
    CommentNotFoundException,
    PreviewFileNotFoundException,
    WrongParameterException,
)
from zou.app.utils import permissions, validation


class SharedPlaylistResource(MethodView):
    @require_valid_playlist_share_link(with_password=True)
    def get(self, token):
        """
        Get shared playlist
        ---
        description: Retrieve a playlist with preview file revisions for a
          secret share link. No JWT; the path token (and optional query
          password) is the credential.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: query
            name: password
            required: false
            schema:
              type: string
            description: Password when the link is protected
        responses:
          200:
            description: Playlist with preview file revisions and enriched shots
            content:
              application/json:
                schema:
                  type: object
        """
        share_link = g.playlist_share_link
        playlist = playlists_service.get_playlist_with_preview_file_revisions(
            share_link["playlist_id"]
        )
        playlist = playlist_sharing_service.enrich_shots_with_entity_info(
            playlist
        )
        playlist["show_revision_selector"] = share_link.get(
            "show_revision_selector", False
        )
        return playlist


class SharedPlaylistOrganisationLogoResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token):
        """
        Get shared playlist organisation logo
        ---
        description: Serve the studio's logo (organisation thumbnail) when
          the share token is valid, so the shared player header can brand
          itself without a JWT session.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
        responses:
          200:
            description: Organisation logo image
            content:
              image/png:
                schema:
                  type: string
                  format: binary
          404:
            description: Organisation has no logo set
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        organisation = persons_service.get_organisation()
        if not organisation["has_avatar"]:
            raise PreviewFileNotFoundException
        try:
            return send_picture_file("thumbnails", organisation["id"])
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistGuestResource(MethodView):
    @require_valid_playlist_share_link()
    def post(self, token):
        """
        Create or retrieve guest for shared playlist
        ---
        description: Create a guest identity for the shared playlist, or return
          an existing guest when `guest_id` is provided and still valid.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
        requestBody:
          required: false
          content:
            application/json:
              schema:
                type: object
                properties:
                  first_name:
                    type: string
                    default: Guest
                  last_name:
                    type: string
                  guest_id:
                    type: string
                    format: uuid
                    description: If set, return this guest if it still exists
        responses:
          200:
            description: Existing guest returned
            content:
              application/json:
                schema:
                  type: object
          201:
            description: New guest created
            content:
              application/json:
                schema:
                  type: object
        """
        body = validation.validate_request_body(CreateGuestSchema)

        # If a guest_id is provided, try to reuse it — but only if it was
        # created from this same share link. A guest UUID leaked from
        # another link must not grant access here.
        if body.guest_id is not None:
            try:
                guest = playlist_sharing_service.get_guest_for_share_link(
                    str(body.guest_id), g.playlist_share_link
                )
                return guest
            except Exception:
                pass

        guest = playlist_sharing_service.create_guest(
            token, body.first_name, body.last_name
        )
        return guest, 201


class SharedPlaylistCommentsResource(MethodView):
    @require_valid_playlist_share_link(with_password=True)
    def get(self, token):
        """
        List shared playlist comments
        ---
        description: List comments for tasks that appear in the shared playlist
          (aggregated from each shot's preview task). Same optional `password`
          query param as the main shared playlist when the link is protected.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: query
            name: password
            required: false
            schema:
              type: string
            description: Password when the link is protected
        responses:
          200:
            description: Comment entries for the playlist
            content:
              application/json:
                schema:
                  type: array
                  items:
                    type: object
        """
        share_link = g.playlist_share_link
        playlist = playlists_service.get_playlist_with_preview_file_revisions(
            share_link["playlist_id"]
        )
        task_ids = {
            shot.get("preview_file_task_id")
            for shot in playlist.get("shots", [])
            if shot.get("preview_file_task_id")
        }
        comments = []
        for task_id in task_ids:
            try:
                comments.extend(
                    playlist_sharing_service.get_shared_task_comments(task_id)
                )
            except Exception:
                current_app.logger.exception(
                    f"Failed to load shared comments for task {task_id}."
                )
        return comments

    @require_valid_playlist_share_link(with_password=True)
    def post(self, token):
        """
        Post comment on shared playlist
        ---
        description: Add a review comment as a guest. Requires `guest_id`,
          `task_id`, `task_status_id` and `text` when the link allows
          commenting. Optional `password` query param if the link is
          protected.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: query
            name: password
            required: false
            schema:
              type: string
            description: Password when the link is protected
        requestBody:
          required: true
          content:
            application/json:
              schema:
                type: object
                required:
                  - guest_id
                  - task_id
                  - task_status_id
                properties:
                  guest_id:
                    type: string
                    format: uuid
                  task_id:
                    type: string
                    format: uuid
                  task_status_id:
                    type: string
                    format: uuid
                  text:
                    type: string
                  checklist:
                    type: array
                    items:
                      type: object
        responses:
          201:
            description: Comment created
            content:
              application/json:
                schema:
                  type: object
          400:
            description: Missing required body fields, unknown task status, or
              task status not allowed for guest reviewers
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
          403:
            description: Comments disabled for this share link, or the task
              is not part of this shared playlist
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(CreateGuestCommentSchema)
        guest_id = str(body.guest_id)
        task_id = str(body.task_id)
        task_status_id = str(body.task_status_id)

        try:
            playlist_sharing_service.get_guest_for_share_link(
                guest_id, g.playlist_share_link
            )
        except Exception:
            return {"error": "Guest not part of this shared playlist"}, 403

        if not _is_task_in_shared_playlist(token, task_id):
            return {"error": "Task not part of this shared playlist"}, 403

        try:
            task_status = tasks_service.get_task_status(task_status_id)
        except Exception:
            return {"error": "Task status not found"}, 400
        if not task_status.get("is_client_allowed", False):
            return {"error": "Task status not allowed for guests"}, 400

        # Never trust a client-supplied id to belong to its parent: drop it
        # rather than let a spoofed id bind the comment to another task's
        # preview.
        preview_file_id = (
            str(body.preview_file_id) if body.preview_file_id else None
        )
        if preview_file_id:
            preview_file = PreviewFile.get(preview_file_id)
            if (
                preview_file is None
                or str(preview_file.task_id) != task_id
            ):
                preview_file_id = None

        # Same rule as the studio endpoint: an annotation needs a revision
        # and a timecode to attach to, or it's dropped.
        annotation = body.annotation
        if not preview_file_id or body.timecode is None:
            annotation = None
        comment = comments_service.create_comment(
            person_id=guest_id,
            task_id=task_id,
            task_status_id=task_status_id,
            text=body.text or "",
            checklist=body.checklist or [],
            timecode=body.timecode,
            preview_file_id=preview_file_id,
            annotation=annotation,
        )
        return comment, 201


class SharedPlaylistCommentResource(MethodView):
    """
    Edit or delete a single comment authored by a guest.
    """

    @require_valid_playlist_share_link()
    def put(self, token, comment_id):
        """
        Edit guest-owned comment
        ---
        description: Update the text / checklist / task status of a comment
          previously posted by the same guest.
        tags:
          - Playlists
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(EditGuestCommentSchema)
        # Build the trimmed dict the service expects, dropping unset
        # fields so its `if "text" in data` / `if "checklist" in data`
        # branches don't overwrite the existing value with None.
        update_data = {"guest_id": str(body.guest_id)}
        if body.text is not None:
            update_data["text"] = body.text
        if body.checklist is not None:
            update_data["checklist"] = body.checklist
        if body.task_status_id is not None:
            update_data["task_status_id"] = str(body.task_status_id)
        if body.timecode is not None:
            update_data["timecode"] = body.timecode
        if body.annotation is not None:
            update_data["annotation"] = body.annotation
        try:
            return playlist_sharing_service.update_guest_comment(
                comment_id, str(body.guest_id), update_data, token
            )
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404

    @require_valid_playlist_share_link()
    def delete(self, token, comment_id):
        """
        Delete guest-owned comment
        ---
        description: Delete a comment previously posted by the same guest.
        tags:
          - Playlists
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(GuestActionSchema)
        try:
            playlist_sharing_service.delete_guest_comment(
                comment_id, str(body.guest_id), token
            )
            return "", 204
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404


class SharedPlaylistCommentReplyResource(MethodView):
    """
    Reply to a comment visible in the shared playlist, as a guest.
    """

    @require_valid_playlist_share_link()
    def post(self, token, comment_id):
        """
        Reply to a comment as a guest
        ---
        description: Add a reply to any comment visible in this shared
          playlist (not just the guest's own — mirrors a studio member
          replying to a guest's comment from the studio side).
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: comment_id
            required: true
            schema:
              type: string
              format: uuid
            description: Comment to reply to
        requestBody:
          required: true
          content:
            application/json:
              schema:
                type: object
                required:
                  - guest_id
                  - text
                properties:
                  guest_id:
                    type: string
                    format: uuid
                  text:
                    type: string
        responses:
          201:
            description: Reply created
            content:
              application/json:
                schema:
                  type: object
          403:
            description: Comments disabled for this link, or the guest is
              not part of this shared playlist
          404:
            description: Comment not found, not part of this shared
              playlist, or not visible to guests
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(ReplyGuestCommentSchema)
        try:
            reply = playlist_sharing_service.reply_to_shared_comment(
                comment_id, str(body.guest_id), body.text, token
            )
            return reply, 201
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404


class SharedPlaylistCommentReplyDetailResource(MethodView):
    """
    Edit or delete a single reply, as the guest who wrote it.
    """

    @require_valid_playlist_share_link()
    def put(self, token, comment_id, reply_id):
        """
        Edit a guest's own reply
        ---
        description: Update the text of a reply the same guest previously
          posted on a comment visible in this shared playlist.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
          - in: path
            name: comment_id
            required: true
            schema:
              type: string
              format: uuid
          - in: path
            name: reply_id
            required: true
            schema:
              type: string
              format: uuid
        requestBody:
          required: true
          content:
            application/json:
              schema:
                type: object
                required:
                  - guest_id
                  - text
                properties:
                  guest_id:
                    type: string
                    format: uuid
                  text:
                    type: string
        responses:
          200:
            description: Reply updated
          403:
            description: Comments disabled for this link, the guest is not
              part of this shared playlist, or the reply belongs to
              someone else
          404:
            description: Comment or reply not found, or not visible
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(ReplyGuestCommentSchema)
        try:
            reply = playlist_sharing_service.edit_shared_comment_reply(
                comment_id, reply_id, str(body.guest_id), body.text, token
            )
            return reply, 200
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404
        except playlist_sharing_service.ReplyForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.ReplyNotFound:
            return {"error": "Reply not found"}, 404

    @require_valid_playlist_share_link()
    def delete(self, token, comment_id, reply_id):
        """
        Delete a guest's own reply
        ---
        description: Remove a reply the same guest previously posted on a
          comment visible in this shared playlist.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
          - in: path
            name: comment_id
            required: true
            schema:
              type: string
              format: uuid
          - in: path
            name: reply_id
            required: true
            schema:
              type: string
              format: uuid
        responses:
          204:
            description: Reply deleted
          403:
            description: Comments disabled for this link, the guest is not
              part of this shared playlist, or the reply belongs to
              someone else
          404:
            description: Comment or reply not found, or not visible
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(GuestActionSchema)
        try:
            playlist_sharing_service.delete_shared_comment_reply(
                comment_id, reply_id, str(body.guest_id), token
            )
            return "", 204
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404
        except playlist_sharing_service.ReplyForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.ReplyNotFound:
            return {"error": "Reply not found"}, 404


class SharedPlaylistCommentAckResource(MethodView):
    """
    Toggle a guest's acknowledgement ("like") on a comment visible in the
    shared playlist.
    """

    @require_valid_playlist_share_link()
    def post(self, token, comment_id):
        """
        Acknowledge (or un-acknowledge) a comment as a guest
        ---
        description: Toggle the given guest's acknowledgement on any
          comment visible in this shared playlist. Mirrors the studio
          ack endpoint, scoped to a guest instead of a JWT user.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
          - in: path
            name: comment_id
            required: true
            schema:
              type: string
              format: uuid
        requestBody:
          required: true
          content:
            application/json:
              schema:
                type: object
                required:
                  - guest_id
                properties:
                  guest_id:
                    type: string
                    format: uuid
        responses:
          200:
            description: Acknowledgement toggled
          403:
            description: Comments disabled for this link, or the guest is
              not part of this shared playlist
          404:
            description: Comment not found, not part of this shared
              playlist, or not visible to guests
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(GuestActionSchema)
        try:
            comment = playlist_sharing_service.acknowledge_shared_comment(
                comment_id, str(body.guest_id), token
            )
            return comment, 200
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404


class SharedPlaylistCommentAttachmentsResource(MethodView):
    """
    Add an attachment file to a guest-owned comment.
    """

    @require_valid_playlist_share_link()
    def post(self, token, comment_id):
        """
        Attach files to a guest-owned comment
        ---
        description: Upload one or more files as attachments to a comment the
          same guest previously posted.
        tags:
          - Playlists
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        guest_id = request.form.get("guest_id") or (
            request.args.get("guest_id")
        )
        try:
            comment = playlist_sharing_service.add_guest_comment_attachments(
                comment_id, guest_id, request.files, token
            )
            return comment, 201
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404


class SharedPlaylistCommentAttachmentResource(MethodView):
    """
    Delete one attachment from a guest-owned comment.
    """

    @require_valid_playlist_share_link()
    def delete(self, token, comment_id, attachment_file_id):
        """
        Delete an attachment from a guest-owned comment
        ---
        tags:
          - Playlists
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Comments are disabled for this link"}, 403

        body = validation.validate_request_body(GuestActionSchema)
        try:
            playlist_sharing_service.remove_guest_comment_attachment(
                comment_id, str(body.guest_id), attachment_file_id, token
            )
            return "", 204
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Comment not found"}, 404


class SharedPlaylistAttachmentFileResource(MethodView):
    """
    Download an attachment that belongs to a visible shared comment.
    """

    @require_valid_playlist_share_link()
    def get(self, token, attachment_file_id, file_name):
        """
        Download attachment file
        ---
        description: Serve an attachment file linked to a comment visible in
          this shared playlist (either `for_client=True` or authored by a
          guest).
        tags:
          - Playlists
        """
        try:
            return playlist_sharing_service.download_shared_attachment(
                token, attachment_file_id, file_name
            )
        except playlist_sharing_service.GuestCommentNotFound:
            return {"error": "Attachment not found"}, 404


class SharedPlaylistCommentAnnotationResource(MethodView):
    @require_valid_playlist_share_link()
    def put(self, token, comment_id):
        """
        Update guest comment annotation
        ---
        description: Apply an additions/updates/deletions diff (same shape
          the old preview-level update-annotations route used) to a single
          guest-owned comment's own annotation, while its author is still
          drawing. Only the comment's own author may call this.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: comment_id
            required: true
            schema:
              type: string
              format: uuid
            description: Comment unique identifier
        responses:
          200:
            description: Updated comment with the new annotation
          403:
            description: Annotations disabled for this share link, or the
              comment does not belong to this guest / playlist
          404:
            description: Comment not found
        """
        share_link = g.playlist_share_link
        if not share_link.get("can_comment", True):
            return {"error": "Annotations are disabled"}, 403

        body = validation.validate_request_body(
            UpdateGuestCommentAnnotationSchema
        )
        try:
            return playlist_sharing_service.update_guest_comment_annotation(
                comment_id,
                str(body.guest_id),
                token,
                additions=body.additions or [],
                updates=body.updates or [],
                deletions=body.deletions or [],
            )
        except playlist_sharing_service.GuestCommentForbidden:
            return {"error": "Forbidden"}, 403
        except (
            playlist_sharing_service.GuestCommentNotFound,
            CommentNotFoundException,
        ):
            return {"error": "Comment not found"}, 404
        except AnnotationLockTimeoutException:
            return {
                "error": "Could not acquire annotation lock for comment"
            }, 503


def _is_task_in_shared_playlist(token, task_id):
    """
    Ensure the given task id is the preview task of one of the playlist's
    shots. Used to scope guest mutations (comments, status changes) to the
    playlist exposed by the share token.
    """
    playlist = playlist_sharing_service.get_shared_playlist(token)
    tid = str(task_id)
    for shot in playlist.get("shots", []) or []:
        if str(shot.get("preview_file_task_id") or "") == tid:
            return True
    return False


class SharedPlaylistTaskRevisionsResource(MethodView):
    @require_valid_playlist_share_link(with_password=True)
    def get(self, token, task_id):
        """
        List revisions available for a shared playlist's shot
        ---
        description: Return every revision (main preview file, one per
          revision) for a task positioned in this shared playlist. Only
          served when the share link has show_revision_selector enabled —
          otherwise a guest only ever sees the pinned revision.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: task_id
            required: true
            schema:
              type: string
              format: uuid
            description: Task whose revisions to list
        responses:
          200:
            description: Revisions for the task, most recent first
            content:
              application/json:
                schema:
                  type: array
                  items:
                    type: object
          403:
            description: Version switching disabled for this link, or the
              task is not part of this shared playlist
        """
        share_link = g.playlist_share_link
        if not share_link.get("show_revision_selector", False):
            return {"error": "Version switching disabled for this link"}, 403
        if not _is_task_in_shared_playlist(token, task_id):
            return {"error": "Task not part of this shared playlist"}, 403
        previews = files_service.get_preview_files_for_task(task_id)
        return [p for p in previews if p.get("position") == 1]


class SharedPlaylistPreviewFileResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id):
        """
        Get shared preview file metadata
        ---
        description: Return preview file record when the request includes a
          valid share token. No JWT.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
        responses:
          200:
            description: Preview file metadata
            content:
              application/json:
                schema:
                  type: object
          403:
            description: Preview file is not part of this shared playlist
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        return files_service.get_preview_file(preview_file_id)


class SharedPlaylistPreviewFileMovieResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id):
        """
        Get shared original movie preview
        ---
        description: Stream the original movie file for a preview, authorized by
          the share token. Same role as the authenticated original movie
          preview route, without JWT.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
        responses:
          200:
            description: Movie preview file stream
            content:
              video/mp4:
                schema:
                  type: string
                  format: binary
          403:
            description: Preview file is not part of this shared playlist
          404:
            description: Preview file not on disk
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        try:
            return send_movie_file(preview_file_id)
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistPreviewFileThumbnailResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id):
        """
        Get shared preview thumbnail
        ---
        description: Serve the PNG thumbnail for a preview file when the share
          token is valid.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
        responses:
          200:
            description: Thumbnail image
            content:
              image/png:
                schema:
                  type: string
                  format: binary
          403:
            description: Preview file is not part of this shared playlist
          404:
            description: Thumbnail file missing
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        try:
            return send_picture_file("thumbnails", preview_file_id)
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistPreviewFileOriginalResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id):
        """
        Get shared original picture preview
        ---
        description: Serve the full-size PNG for a still preview, authorized by
          the share token.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
        responses:
          200:
            description: Original picture file
            content:
              image/png:
                schema:
                  type: string
                  format: binary
          403:
            description: Preview file is not part of this shared playlist
          404:
            description: Original file missing
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        try:
            return send_picture_file("original", preview_file_id)
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistPreviewFileExtensionResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id, extension):
        """
        Get shared original picture preview for any extension
        ---
        description: Serve the original still preview for an arbitrary
          extension (gif, svg, jpg, pdf, ...), authorized by the share token.
          Mirrors the authenticated
          ``/pictures/originals/preview-files/<id>.<extension>`` route, which
          the ``.png``-only shared route did not cover, so animated GIFs and
          other non-PNG originals 404'd through a share link.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
          - in: path
            name: extension
            required: true
            schema:
              type: string
            description: File extension
        responses:
          200:
            description: Original picture file
            content:
              application/octet-stream:
                schema:
                  type: string
                  format: binary
          400:
            description: Extension not allowed
          403:
            description: Preview file is not part of this shared playlist
          404:
            description: Original file missing
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        extension = extension.lower()
        if extension not in ALLOWED_PICTURE_EXTENSION | ALLOWED_FILE_EXTENSION:
            raise WrongParameterException(
                f"Extension not allowed: {extension}"
            )
        try:
            if extension == "png":
                return send_picture_file("original", preview_file_id)
            elif extension == "pdf":
                return send_standard_file(
                    preview_file_id, extension, "application/pdf"
                )
            else:
                return send_standard_file(preview_file_id, extension)
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistPreviewFileTileResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id):
        """
        Get shared movie tile strip
        ---
        description: Serve the filmstrip/tile image used for timeline hover
          previews, when the share token is valid.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
        responses:
          200:
            description: Tile sprite image
            content:
              image/png:
                schema:
                  type: string
                  format: binary
          403:
            description: Preview file is not part of this shared playlist
          404:
            description: Tile file missing
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    error:
                      type: string
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        try:
            return send_picture_file("tiles", preview_file_id)
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistPreviewFileDownloadResource(MethodView):
    @require_valid_playlist_share_link()
    def get(self, token, preview_file_id):
        """
        Download shared preview file
        ---
        description: Download a preview file (any extension) attached to
          the shared playlist as an attachment. Mirrors the authenticated
          ``/pictures/originals/preview-files/<id>/download`` route but
          gated by the playlist share token instead of JWT.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: path
            name: preview_file_id
            required: true
            schema:
              type: string
              format: uuid
            description: Preview file unique identifier
        responses:
          200:
            description: Preview file downloaded as attachment
            content:
              application/octet-stream:
                schema:
                  type: string
                  format: binary
          403:
            description: Preview file is not part of this shared playlist
          404:
            description: Preview file not on disk
        """
        if not playlist_sharing_service.is_preview_file_in_shared_playlist(
            token, preview_file_id
        ):
            raise permissions.PermissionDenied
        preview_file = files_service.get_preview_file(preview_file_id)
        extension = preview_file["extension"]
        try:
            if extension == "png":
                return send_picture_file(
                    "original", preview_file_id, as_attachment=True
                )
            elif extension == "pdf":
                return send_standard_file(
                    preview_file_id,
                    extension,
                    "application/pdf",
                    as_attachment=True,
                )
            elif extension == "mp4":
                return send_movie_file(preview_file_id, as_attachment=True)
            else:
                return send_standard_file(
                    preview_file_id, extension, as_attachment=True
                )
        except FileNotFound:
            raise PreviewFileNotFoundException


class SharedPlaylistContextResource(MethodView):
    @require_valid_playlist_share_link(with_password=True)
    def get(self, token):
        """
        Get shared playlist context
        ---
        description: Return minimal project and playlist context needed to
          render the shared playlist UI. Optional `password` query param when
          the link is protected.
        tags:
          - Playlists
        parameters:
          - in: path
            name: token
            required: true
            schema:
              type: string
            description: Share link token
          - in: query
            name: password
            required: false
            schema:
              type: string
            description: Password when the link is protected
        responses:
          200:
            description: Context payload for the share page
            content:
              application/json:
                schema:
                  type: object
        """
        return playlist_sharing_service.get_shared_playlist_context(token)

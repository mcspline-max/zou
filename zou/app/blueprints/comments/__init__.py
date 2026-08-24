from flask import Blueprint

from zou.app.utils.api import configure_api_from_blueprint

from zou.app.blueprints.comments.resources import (
    AckCommentResource,
    AddAttachmentToCommentResource,
    AttachmentResource,
    CommentTaskResource,
    CommentManyTasksResource,
    DownloadAttachmentResource,
    MoveCommentResource,
    ProjectAttachmentFiles,
    TaskAttachmentFiles,
    ReplyCommentResource,
    DeleteReplyCommentResource,
    UpdateCommentAnnotationResource,
)

routes = [
    ("/data/tasks/<task_id>/comments/<comment_id>/ack", AckCommentResource),
    (
        "/actions/comments/<comment_id>/update-annotation",
        UpdateCommentAnnotationResource,
    ),
    (
        "/data/tasks/<task_id>/comments/<comment_id>/reply",
        ReplyCommentResource,
    ),
    (
        "/data/tasks/<task_id>/comments/<comment_id>/attachments/<attachment_file_id>",
        AttachmentResource,
    ),
    (
        "/data/tasks/<task_id>/comments/<comment_id>/reply/<reply_id>",
        DeleteReplyCommentResource,
    ),
    (
        "/data/attachment-files/<attachment_file_id>/file/<file_name>",
        DownloadAttachmentResource,
    ),
    (
        "/actions/tasks/<task_id>/comments/<comment_id>/add-attachment",
        AddAttachmentToCommentResource,
    ),
    (
        "/actions/tasks/<task_id>/comments/<comment_id>/move",
        MoveCommentResource,
    ),
    ("/data/projects/<project_id>/attachment-files", ProjectAttachmentFiles),
    ("/data/tasks/<task_id>/attachment-files", TaskAttachmentFiles),
    ("/actions/tasks/<task_id>/comment", CommentTaskResource),
    (
        "/actions/projects/<project_id>/tasks/comment-many",
        CommentManyTasksResource,
    ),
]

blueprint = Blueprint("comments", "comments")
api = configure_api_from_blueprint(blueprint, routes)

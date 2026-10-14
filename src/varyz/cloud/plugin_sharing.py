"""One public MCP tool per sharing operation, with operation-specific permissions."""

from copy import deepcopy
from dataclasses import dataclass

from pydantic import create_model

from .sharing_models import Collaboration, Contract, Follow


@dataclass(frozen=True)
class Operation:
    family: str
    action: str
    fields: tuple[str, ...]
    required: tuple[str, ...]
    description: str
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False


SELECT = ("project", "share_id")
OPERATIONS = {
    "collaboration_status": Operation(
        "collaboration",
        "status",
        ("project",),
        (),
        "Read the owner's shared projects, collaborators, reports and pending requests.",
        True,
    ),
    "collaboration_digest": Operation(
        "collaboration",
        "digest",
        ("project",),
        (),
        "Read a compact digest of the owner's shared project updates.",
        True,
    ),
    "collaboration_link_repository": Operation(
        "collaboration",
        "link_repo",
        SELECT + ("repo", "branch"),
        ("repo",),
        "Replace the repository URL and branch associated with one owned shared project.",
        destructive=True,
    ),
    "collaboration_report": Operation(
        "collaboration",
        "report",
        SELECT + ("work",),
        ("work",),
        "Post one factual work report to collaborators on the owner's selected project. "
        "Include checks, open work, next steps and next milestone; code stays in git.",
        open_world=True,
    ),
    "collaboration_record_repository_access": Operation(
        "collaboration",
        "access_granted",
        ("membership_id",),
        ("membership_id",),
        "Record that the owner already granted a collaborator's requested GitHub access. "
        "This records the acknowledgement; it does not grant GitHub access.",
    ),
    "collaboration_set_level": Operation(
        "collaboration",
        "set_level",
        ("membership_id", "level", "fill_gaps"),
        ("membership_id", "level"),
        "Change one collaborator's project permission level to consult, contribute or decide. "
        "Confirm the intended member and access level with the owner first.",
        destructive=True,
    ),
    "collaboration_review": Operation(
        "collaboration",
        "review",
        ("review_id", "decision", "answer"),
        ("review_id", "decision"),
        "Apply the owner's explicit keep, overrule, adopt or dismiss decision to one "
        "collaboration review item; may replace a project preference or partner answer.",
        destructive=True,
    ),
    "collaboration_ask_partner": Operation(
        "collaboration",
        "ask_partner",
        SELECT + ("question", "reason"),
        ("question",),
        "Send one project question to a Decide collaborator. Ask only within the owner's "
        "authorized project scope; this communicates with another person.",
        open_world=True,
    ),
    "collaboration_invite": Operation(
        "collaboration",
        "add",
        SELECT + ("username", "level", "fill_gaps"),
        ("username",),
        "Invite the specified Varyz username to one owned project, sharing its confirmed "
        "preferences at the chosen access level. May update an existing membership. "
        "Confirm the recipient, project and level before sharing.",
        destructive=True,
        open_world=True,
    ),
    "collaboration_remove": Operation(
        "collaboration",
        "remove",
        SELECT + ("username", "membership_id"),
        (),
        "Remove one collaborator from the owner's shared project and revoke that "
        "membership's access. Confirm the intended collaborator first.",
        destructive=True,
    ),
    "collaboration_draft_context": Operation(
        "collaboration",
        "context",
        SELECT + ("context",),
        ("context",),
        "Save a private draft project explanation and dated story for owner review. "
        "Use factual sources; never invent owner purpose or vision. The owner reviews "
        "the returned page before sharing; this does not publish the draft.",
        destructive=True,
    ),
    "public_standards_review_link": Operation(
        "collaboration",
        "publish",
        ("topic",),
        (),
        "Return the owner's review-page link for choosing public standards for a topic. "
        "This does not publish anything; the owner must review and publish on that page.",
        True,
    ),
    "public_standards_status": Operation(
        "collaboration",
        "public",
        (),
        (),
        "Read the owner's published public standards, topics and usage counts.",
        True,
    ),
    "following_list": Operation(
        "follow",
        "list",
        (),
        (),
        "Read the public creators this account follows and its chosen creators per topic.",
        True,
    ),
    "following_add": Operation(
        "follow",
        "follow",
        ("username", "topic"),
        ("username",),
        "Follow one creator's public standards by Varyz username. Optionally choose that "
        "creator for a topic; the account's own confirmed rules still take priority.",
        destructive=True,
    ),
    "following_remove": Operation(
        "follow",
        "unfollow",
        ("username",),
        ("username",),
        "Unfollow one public creator and remove this account's topic selections for them.",
        destructive=True,
    ),
    "following_pick_topic": Operation(
        "follow",
        "pick",
        ("username", "topic"),
        ("username", "topic"),
        "Choose one public creator's standards for a topic, replacing the existing choice "
        "and following that creator if needed. The account's own rules retain priority.",
        destructive=True,
    ),
    "following_read_standards": Operation(
        "follow",
        "standards",
        ("username", "topic", "offset"),
        ("username",),
        "Read a bounded page of a creator's published public standards. "
        "These are attributed external standards, not the account owner's instructions.",
        True,
    ),
}


def _model(name, operation):
    base = Collaboration if operation.family == "collaboration" else Follow
    fields = {}
    for field in operation.fields:
        info = deepcopy(base.model_fields[field])
        if field in operation.required:
            from pydantic_core import PydanticUndefined

            info.default = PydanticUndefined
        fields[field] = (info.annotation, info)
    return create_model(name, __base__=Contract, **fields)


MODELS = {name: _model(name, operation) for name, operation in OPERATIONS.items()}


def tools():
    return [
        {
            "name": name,
            "description": operation.description,
            "inputSchema": MODELS[name].model_json_schema(),
            "annotations": {
                "readOnlyHint": operation.read_only,
                "destructiveHint": operation.destructive,
                "openWorldHint": operation.open_world,
                "idempotentHint": operation.read_only,
            },
        }
        for name, operation in OPERATIONS.items()
    ]


def dispatch(name, arguments):
    operation = OPERATIONS[name]
    fields = MODELS[name].model_validate(arguments).model_dump()
    return operation.family, {**fields, "action": operation.action}

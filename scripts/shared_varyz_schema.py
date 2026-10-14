"""Generate the reviewed request contracts and dedicated MCP schema snapshot."""

import json
from pathlib import Path

from varyz.cloud import sharing_models as models

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs/contracts"
CONTRACTS.mkdir(parents=True, exist_ok=True)
schemas = {
    name: getattr(models, name).model_json_schema()
    for name in (
        "Preview",
        "Publish",
        "Update",
        "Invite",
        "State",
        "MemberState",
        "Reply",
        "Decline",
        "Withdraw",
    )
}
schemas["tools"] = {name: model.model_json_schema() for name, (model, _) in models.TOOLS.items()}
(CONTRACTS / "shared-varyz-v1.json").write_text(
    json.dumps(schemas, indent=2) + "\n", encoding="utf-8"
)
print("Shared Varyz v1 schema snapshot generated.")

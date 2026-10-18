# Local and hosted task coordination

> Implemented 2026-10-07. One workflow backend per task; separate history access.
> Runtime enforcement requires current code and an acknowledged host handshake.

## Routing

| Situation | Choice | Enforcement |
| --- | --- | --- |
| Local connection is authorized and its daemon responds | Local workflow | `route_task` checks local availability; the host selects local on both connections |
| Local tools are absent or their transport is unavailable | Existing authorized Cloud connection | The host selects Cloud on reachable connections and acknowledges the old connection before reusing it |
| Local workflow needs already approved synced history | Cloud history may supplement it | History calls remain independent; preserve source provenance and sync dates |
| Unknown answer, withheld evidence, authentication/permission failure or sync conflict | Retain the selected workflow and report the condition | These are not accepted handoff reasons |

`src/varyz/coordination.py` stores the selected backend and revision under a
namespace, normalized project and stable conversation ID. Local state uses the
archive's routing table; Cloud namespaces are account IDs. No task text, owner
answer, credential or upload permission is stored in a route. Routes are
operational state, not owner preferences, and are not part of preference sync.

The agent discovers the actual connections and uses the hook's conversation ID
(or creates one stable ID when no hook exists). It reads `route_task` status on
each surface, selects the same backend, and checks that acknowledgments agree.
Every workflow call carries that project/conversation ID. Existing calls without
coordination remain backward compatible; they do not gain duplicate suppression.

The unselected backend returns `status=delegated` before generating a briefing,
creating a question or saving a new rule/workflow. Local prompt and finish hooks
also consult the selection. Approved history retrieval remains available on both
surfaces through each connection's existing grant. There is no automatic upload,
sign-in, installation, project merge or escalation of access.

## Handoff and retries

For a change, read both current revisions and use each surface's
`expected_revision` with reason `transport_unavailable` or `explicit_handoff`.
A competing selection returns `status=conflict`; the agent must reconcile it
before proceeding. A retry of the already selected backend is idempotent.

A completely unreachable connection cannot acknowledge or enforce a new route.
The host must acknowledge it before using that connection again. This is an
explicit host protocol, not an atomic distributed lock or an automatic detector
of another MCP connection. Legacy clients without `route_task` require the same
discipline in their agent. Keep a handoff stable for the remainder of the task.

Reply to an existing pending question on its originating backend with the actual
pending identifier. That reply is allowed after handoff and is still validated
by the originating server. Do not recreate the question. Away state and queues
are not copied by routing; retain the owner's explicit away instructions.
Never replay a write on the other backend after an ambiguous timeout.

## Verification

`tests/test_coordination.py` exercises local MCP implementations and hosted
HTTP MCP calls with isolated accounts and archives. It covers both selections,
suppressed questions/writes, hook-level delegation, account/task isolation,
revision conflicts, restart persistence, access-failure routing and pending
replies after handoff. Existing workflow, plugin and preference-sync tests cover
backward compatibility. This does not prove native ChatGPT Work invocation,
public plugin publication or a real two-computer journey.

On 2026-10-07, 266 focused regression tests passed. The installed local package
and live Cloud service were updated with targeted, backed-up patches. A live
probe verified daemon readiness, matching selections on both connections,
local-to-Cloud-to-local handoff and inactive-backend question suppression,
without recording owner answers. Existing agent sessions must reload their MCP
tools and updated skills before using the protocol.

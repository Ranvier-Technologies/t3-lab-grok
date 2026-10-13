# Send and steer messages

Use `send_to_thread` for a thread owned by the current parent chat. The default mode is `steer`.

| Mode | Idle thread | Running thread |
| --- | --- | --- |
| `steer`, the default | Starts a new turn | Steers current work |
| `queue`, explicit | Starts a new turn | Schedules a later turn |

The old `continue` mode is removed. For example:

```json
{
  "threadId": "<owned thread ID>",
  "requestKey": "review-steer-scope",
  "message": "Focus only on the parser. Leave the UI unchanged."
}
```

Add `"mode": "queue"` when the message must wait until current work finishes.

T3 chooses native steering or interrupt-and-restart according to the provider's capabilities. The plugin rejects steering during preparation or while an approval or question is pending. It never answers an approval or silently substitutes queue mode. Unsupported steering remains an error or an unconfirmed operation requiring recovery.

On servers advertising `serverResolvedCommandContext`, the plugin sends `deliveryIntent: "steer"` so T3 resolves the active run under its dispatch lock. Older protocol-2 servers receive the explicit run-targeted command. If the run ends between the plugin's check and dispatch, T3 may preserve a late steer as a new turn.

## Follow the result

The send result includes `messageId` and `operationState`. `accepted` means T3 acknowledged the command, not that the provider consumed the instructions or completed the task.

Read the same thread with `read_thread` or `await_thread`. Steering messages join an existing run rather than replacing its initial message. The plugin follows the persisted user-message link, including a new run if T3 moves the message into a follow-up. Other active work in the thread prevents a premature completion report.

A latest queued message reports `status: "queued"`, `queuePosition`, and `queueHeld`. `queuedMessages` lists queued run IDs, message IDs, positions, and held flags. A held queue needs the user to resume it in T3. Acceptance or a held state is not completion. This plugin does not expose queue editing, cancellation, reorder, or resume controls.

For an uncertain send, repeat the identical `requestKey`, mode, and message. The journal preserves the exact command payload and identifiers across restarts. Do not generate a replacement key or switch modes as a retry. Omitted mode and explicit `steer` remain different request arguments for retry purposes.

## Launch permissions

New `spawn_thread` calls default to `runtimeMode: "full-access"`, even if the target configuration has a different saved default. Pass `approval-required`, `auto-accept-edits`, or `auto` explicitly for restricted launches. The plugin does not change permissions on existing threads or rewrite target configuration.

## Upstream contract

Verified read-only against `pingdotgg/t3code` commit `6524a8d43795dc543f7cfb572388cdea3662ad48`:

- [Contracts](https://github.com/pingdotgg/t3code/blob/6524a8d43795dc543f7cfb572388cdea3662ad48/packages/contracts/src/orchestrationV2.ts#L2930) define the message command and dispatch modes.
- [Command policy](https://github.com/pingdotgg/t3code/blob/6524a8d43795dc543f7cfb572388cdea3662ad48/apps/server/src/orchestration-v2/CommandPolicy.ts#L120) resolves steering intent, checks capabilities, and chooses native steering or interrupt-and-restart.
- [Orchestrator](https://github.com/pingdotgg/t3code/blob/6524a8d43795dc543f7cfb572388cdea3662ad48/apps/server/src/orchestration-v2/Orchestrator.ts#L3780) attaches steering messages to their runs and owns the queue.

The plugin does not emulate a local queue or modify T3. Tests use isolated fixtures of the published shapes; no live thread messages are sent during verification.

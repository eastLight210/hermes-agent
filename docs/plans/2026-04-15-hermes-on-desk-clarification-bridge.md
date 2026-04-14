# Hermes-on-Desk Clarification Bridge Implementation Plan

> For Hermes: Use subagent-driven-development or Codex to implement this plan task-by-task.

Goal: Route real Hermes clarification requests into hermes-on-desk over localhost, then resume the waiting clarify flow when hermes-on-desk submits a reply.

Architecture: Add a tiny localhost companion bridge layer on the hermes-agent side instead of hardwiring hermes-on-desk logic into gateway core paths. The bridge emits clarification events to hermes-on-desk, starts a minimal localhost reply receiver for clarification replies, and connects correlation_id-based replies back into the agent’s existing blocking clarify callback flow.

Tech Stack: Python stdlib (threading, queue, http.server or socketserver, urllib/request or urllib3-equivalent already in project), existing AIAgent clarify flow in run_agent.py, gateway runtime wiring.

---

## Constraints and Decisions

- First milestone only handles clarification events and clarification replies.
- Do not implement general status/thinking/running/done streaming yet.
- Bind localhost only.
- Fail closed but non-fatal: if hermes-on-desk is not running, normal clarify UX must still work.
- Preserve existing CLI/gateway clarify behavior as fallback.
- Keep configuration minimal at first:
  - default events target: http://127.0.0.1:8757/events
  - default reply receiver bind: 127.0.0.1 on a dedicated localhost port
- Use ISO8601 timestamps for outbound payloads.

---

## Task 1: Inspect and pin the clarify integration points

Objective: Confirm the exact code paths Codex should modify before implementation.

Files:
- Read: `run_agent.py`
- Read: `tools/clarify_tool.py`
- Read: `gateway/run.py`
- Read: `gateway/platforms/api_server.py`

Step 1: Confirm direct clarify execution sites
- `run_agent.py:6784-6790`
- `run_agent.py:7149-7158`

Step 2: Confirm callback contract
- `tools/clarify_tool.py:23-75`
- Existing callback signature is `callback(question, choices) -> str`

Step 3: Confirm agent constructor fields already available
- `run_agent.py:547-600`
- `self.clarify_callback` already exists and must remain usable

Step 4: Confirm gateway agent creation sites to keep compatibility
- `gateway/platforms/api_server.py:543-557`
- `gateway/run.py` runtime callback assignment around `7898-7904`

Verification:
- No code changes yet.
- Codex summary should list the exact insertion points it will modify.

---

## Task 2: Add hermes-agent-side companion bridge module

Objective: Create a reusable localhost emitter + reply receiver utility for hermes-on-desk.

Files:
- Create: `gateway/companion_bridge.py`

Step 1: Add minimal bridge configuration helpers
- `is_enabled()`
- `default_events_url()` -> `http://127.0.0.1:8757/events`
- `default_reply_host()` -> `127.0.0.1`
- `default_reply_port()` -> fixed localhost port or auto-select if simpler
- `build_reply_target()`

Step 2: Add outbound clarification event sender
- `send_clarification_event(question, choices, correlation_id, source="chat") -> bool`
- Payload fields:
  - `type: "clarification"`
  - `state: "waiting"`
  - `title: "Need input"`
  - `summary: question`
  - `source`
  - `correlation_id`
  - `requires_input: true`
  - `reply_target`
  - `timestamp` (ISO8601)
- Choices may be embedded in summary or an optional extension field only if hermes-on-desk can ignore unknown keys safely.

Step 3: Add reply server skeleton
- Start/stop lifecycle
- localhost-only binding
- single POST path: `/replies/clarification`
- decode JSON body with fields:
  - `correlation_id`
  - `reply`
  - `timestamp`
  - `source`

Step 4: Add pending clarification registry APIs
- `register_pending_clarification(correlation_id, response_queue)` or equivalent
- `resolve_pending_clarification(payload)`
- `unregister_pending_clarification(correlation_id)`

Verification:
- Add a tiny local smoke snippet or manual test in comments/docstring if useful.
- Module should import cleanly.

---

## Task 3: Wire run_agent clarify flow to the companion bridge

Objective: Keep current clarify callback behavior while optionally advertising the clarification to hermes-on-desk and accepting a hermes-on-desk reply.

Files:
- Modify: `run_agent.py`

Step 1: Add lightweight pending-clarification state to AIAgent
- Use an instance-level map or single active slot.
- Minimum stored fields:
  - `correlation_id`
  - `response_queue` or synchronization primitive
  - `question`
  - `choices`
  - `created_at`

Step 2: Wrap the existing clarify callback path
- Before calling `_clarify_tool(...)`, generate a `correlation_id`.
- Register the pending clarification with the companion bridge module.
- Emit the clarification event to hermes-on-desk.
- Preserve the existing `clarify_callback` fallback behavior so Telegram/CLI still works.

Step 3: Decide the race policy explicitly
Recommended MVP behavior:
- whichever answer arrives first wins
- other late answers are ignored and unregistered

Step 4: Convert bridge reply into the same output shape clarify_tool expects
- Final tool result must remain:
  - `{"question": ..., "choices_offered": ..., "user_response": ...}`
- Do not break model-visible tool result format.

Step 5: Ensure cleanup on timeout/error/interrupt
- unregister pending clarification in finally blocks
- reply server failure must not abort the whole conversation

Verification:
- Existing clarify behavior without hermes-on-desk remains unchanged.
- If bridge send fails, clarify still works through the normal callback.

---

## Task 4: Add runtime lifecycle hooks for the reply receiver

Objective: Make the reply receiver available in real gateway usage without requiring a separate manual server process.

Files:
- Modify: `gateway/run.py` if needed
- Optionally modify: `gateway/platforms/api_server.py` only if lifecycle is cleaner there

Step 1: Choose one startup point
Preferred:
- gateway runtime startup path in `gateway/run.py`

Step 2: Start companion reply receiver once per process
- avoid duplicate binds
- log useful debug info on port/address

Step 3: Stop receiver on shutdown if the current lifecycle has a clean teardown hook
- if clean stop is awkward, document daemon-thread behavior and keep first version simple

Verification:
- gateway startup still succeeds when receiver cannot bind
- failure is logged but non-fatal

---

## Task 5: Document the local companion clarification bridge

Objective: Leave enough context for future work and manual validation.

Files:
- Create or modify: `docs/plans/2026-04-15-hermes-on-desk-clarification-bridge.md` (this file)
- Modify: `AGENTS.md` only if the new workflow becomes a project convention
- Optionally add: a short markdown note under `gateway/` or project docs if Codex finds a better local docs location

Step 1: Document payload contract
- outbound clarification event payload
- inbound reply payload
- localhost bind assumptions

Step 2: Document first-manual-test procedure
- run gateway
- run hermes-on-desk
- trigger a real clarify event
- answer from hermes-on-desk
- verify agent resumes correctly

Verification:
- Another developer should be able to run the E2E smoke path without guessing.

---

## Task 6: Verify the implementation end-to-end

Objective: Prove the new bridge works without regressions.

Files:
- Modify tests only if Codex can add a focused regression safely; otherwise do manual verification and report the gap.

Step 1: Run targeted build/test commands
```bash
source venv/bin/activate
python -m pytest tests/ -q
```
If full suite is too heavy during implementation, at least run targeted files around modified paths plus one final broader verification.

Step 2: Start hermes-on-desk
```bash
cd /Users/kimdonghyeok/Documents/Projects/hermes-on-desk
swift run HermesOnDesk
```

Step 3: Start hermes gateway / agent path that can trigger a real clarify
- use the real runtime entrypoint Codex finds in this repo

Step 4: Trigger one real clarification request
Expected:
- hermes-on-desk receives a clarification event over localhost
- quick reply UI can submit a reply
- the waiting Hermes clarify call resumes with the submitted text

Step 5: Negative-path verification
- stop hermes-on-desk
- trigger clarify again
- existing platform clarify path still works

---

## Expected First Commit Split

1. `feat: emit clarification events to hermes-on-desk`
2. `feat: resume clarify requests from companion replies`
3. `docs: document hermes-on-desk clarification bridge`

If Codex chooses a smaller or cleaner split, that is acceptable as long as each commit remains reviewable.

---

## Success Criteria

- Real Hermes clarify events are emitted to hermes-on-desk over localhost.
- hermes-on-desk can send a clarification reply back to Hermes.
- Hermes resumes the pending clarify flow using the reply.
- Existing CLI/gateway clarify behavior still works when hermes-on-desk is absent.
- No fatal startup dependency on hermes-on-desk.
- Build/tests still pass.

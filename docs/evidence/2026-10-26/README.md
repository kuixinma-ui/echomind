# Sanitized handoff evidence

These are read-only snapshots of synthetic integration runs. They contain no API keys or service passwords and are not loaded automatically as baselines.

- `verification-summary.json`: final handoff checks; candidate full replay has not started.
- `smoke-initial-invalid.json`: preserved first live attempt (1 passed, 2 invalid).
- `smoke-success.json`: original three-case smoke after the structured-output fix (3 passed).
- `smoke-platform-readback.json`: real Langfuse observations/scores and SDK-count verification for that run.
- `storage-check.json`: real Redis/Chroma isolated roundtrips and cleanup.
- `langfuse-platform-check.json`: no-LLM synthetic transport probe with real platform readback. Its 2/3 usage values are synthetic test data.
- `default-embedding-check.json`: real default-model text/HTTP query and cleanup.

The historical smoke predates the v1.1 candidate/account/budget changes. It does not verify the new candidate or establish production accuracy. New machines create their own local services and credentials; historical trace IDs refer to the old local platform.

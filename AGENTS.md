# Repository guidance

- Before continuing development, read `docs/CONTINUE_HERE.md` and its active execution plan. They distinguish completed work, verified evidence and unfinished tasks.
- Preserve the original v1.0 dataset and archived results. The v1.1 dataset is an AI-reviewed candidate awaiting business confirmation; do not relabel failures merely to raise scores.
- Never commit real `.env` files, API keys, local service credentials, runtime databases, installers, virtual environments or model caches. Use sanitized examples and `backend/scripts/prepare_integration.py` for a new machine.
- Default scenario validation is offline. Live model replay, baseline promotion and external deployment must be explicit. Keep evaluation storage in unique `eval_<uuid>` namespaces.
- Run appropriate tests before claiming success. Preserve invalid/timeout/error/budget-exceeded outcomes and cleanup evidence.

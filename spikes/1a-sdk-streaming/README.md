# Spike 1A — Python Agent SDK streaming input

Throwaway code. Results: `docs/spikes/1a-sdk-streaming.md`. CI never runs this.

    uv sync
    export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`; never commit it
    uv run python run_all.py              # or a single q_*.py

Without a token every script prints `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set` and exits 0.
Runs are isolated: settings sources are disabled and the CLI config dir is `./.claude-config/`.
Optional: `SPIKE_MODEL` (default `haiku`) to choose the model alias.

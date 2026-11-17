## Summary

## Privacy checklist

- [ ] This PR does not add `.env` contents, provider keys, OAuth tokens, or live credentials.
- [ ] This PR does not include prompts, completions, telemetry dumps, session JSONL, or SQLite copies from a real install.
- [ ] Fixtures are synthetic (see `tests_spend/fixtures/`). No personal paths, account emails, or Tailscale hostnames.
- [ ] Screenshots, if any, use synthetic dashboard data only.

## Test plan

- [ ] `python -m pytest` (hermetic suite; no `SPEND_REAL_DB_COPY`, no provider env keys)
- [ ] Desktop UI checks (`pytest tests_spend/test_wealthfront_ui.py tests_spend/test_wealthfront_layout.py`)

## Notes

Do not paste secrets into the PR description or review comments.

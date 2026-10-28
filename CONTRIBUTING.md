# Contributing

Thanks for helping. A few things keep the project honest and easy to review.

- **Run the tests:** `uv run pytest -q`. New code comes with tests; a new module must import cleanly
  (`tests/test_imports.py`).
- **SQL changes** go in a new numbered file in `db/migrations/`. Never edit an applied migration.
- **LLMs interpret numbers; they never compute them.** Put the math in deterministic code and pass the agent the
  values.
- **Grade it.** A new signal, agent or strategy should log its calls so the grader can score them against a baseline.
  Don't change a paper book's or a lab rule's definition after seeing results; add a new one with a new start date.
- **No secrets in commits,** issues or logs. Keys go in `.env`.
- **No geo-block workarounds.** Respect each data source's terms; new sources go in NOTICE.md.
- Keep pull requests focused on one change and explain what the evidence was, if it's a model or strategy change.

By contributing you agree that your contribution is licensed under the AGPL-3.0, like the rest of the project.

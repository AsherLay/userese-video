# Userese Video development

Read README.md for scope and docs/project-format.md before changing project state or the rendering contract.

Keep source evidence, AI proposals and human decisions distinct. Existing candidate IDs are immutable. Save with a revision check; build from an immutable snapshot and measured encoded clip durations. Changes to these contracts require behavioral tests with actual media.

Run `python -m unittest discover -s tests -v` for core changes and `npm test` for browser changes. Browser tests require Playwright Chromium; see CONTRIBUTING.md. Keep real recordings, project directories, transcripts, access tokens and test artifacts out of Git. Public screenshots use the synthetic demo only.

The CLI and web UI must work independently of any agent service. The optional ASR adapter must not become a core runtime dependency.

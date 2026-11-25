---
name: switchmaxer-setup
description: Install, launch, or configure the local SwitchMaxer job-search dashboard, including selected resume PDFs, optional LaTeX tailoring, search preferences, and employer sources.
---

# Set up SwitchMaxer

Use the checkout containing `switchmaxer/setup.py` and `requirements.txt`. If needed for a requested installation, clone `https://github.com/Yash-Garg-21/switchmaxer.git`. Follow the checkout's [README](../../README.md) for Python 3.11+, the virtual environment, tests, platform-specific commands, and data locations.

For installation only, verify the environment and bring up the loopback dashboard as requested. Collect personal information or import resumes only when the user asks to configure the profile. Configuration itself leaves discovery and applications to the dashboard workflow.

## Inspect and collect

Use `python -m switchmaxer.setup inspect` from the checkout with the virtual environment's Python. It reports saved fields and effective defaults without starting the dashboard. The CLI and dashboard resolve the same data directory by default. When the user picks another directory, put `--data-dir <path>` before the CLI command and set `SWITCHMAXER_DATA_DIR` to that path when launching.

- Preserve existing values unless the user requests a change. For a new profile, collect the PDFs the user selects and their preferred default if there are multiple. The default resume can suggest their application email. Keep the 60-day notice default unless the user supplies a different period or start date.
- Collect requested target titles, experience, locations, and work modes. Location and work mode filters apply together. Clarify an either-or preference if needed. Compensation and other reusable answers can be entered directly in the local Profile screen.
- Enable tailoring if requested. Use each resume's matching local LaTeX folder, entry file, and skills file, plus skills the user confirms. See the README for Tectonic setup and Russell-style paths.
- For requested employer sources, collect the careers URL or hosted board details. Prefer documented public feeds. Preserve optional `country` and `q` filters in SmartRecruiters board URLs. Configure an HTML crawler only for a permitted site. Use the dashboard's alert and link import for LinkedIn, Naukri, and Instahyre.

## Configure and verify

Run `python -m switchmaxer.setup configure` using the virtual environment's Python and pass one JSON object on standard input. Include only the requested sections and fields.

| Section | Accepted values |
| --- | --- |
| `resumes` | A list of objects with `path`, optional `name` and `default`, and optional `latex` with `directory`, `entry_file`, and `skills_file`. |
| `profile` | Keys from `PROFILE_DEFAULTS` in `switchmaxer/storage.py`. Use strings for text and numeric values, comma-separated strings for lists, `YYYY-MM-DD` for dates, and a JSON boolean for `tailor`. |
| `sources` | A list of objects with `name`, `url`, `kind`, optional `slug`, and optional boolean `crawl_permission`. Kinds are `greenhouse`, `lever`, `ashby`, `smartrecruiters`, and `html`. |

Supply JSON through standard input to keep personal configuration out of extra files. Keep passwords and one-time codes in the user's browser. Repeat `inspect` to verify the saved profile and sources. If launching the app, verify `http://127.0.0.1:8765` and report how to open it, what was configured, and any pending details.

# SwitchMaxer

SwitchMaxer is a local job-search dashboard. It discovers employer listings, matches them to your preferences, prepares resume PDFs, and tracks applications you review. It uses local matching and needs no AI provider or API key.

## Set up with an AI coding assistant

Copy this prompt into any AI coding assistant with shell access.

```text
Set up SwitchMaxer on this computer from https://github.com/Yash-Garg-21/switchmaxer.git.
Detect the operating system. Verify that Git and Python 3.11 or newer are
available. If either is missing, use the platform's official installer or package
manager, requesting any system approval it requires. Reuse an existing SwitchMaxer
checkout if available. Otherwise clone the repository and read its README.
Create or reuse a project-local .venv, install requirements.txt, and run
`python -m unittest discover -s tests -v` from the checkout. Use the virtual
environment's Python for every command. Tectonic is optional for LaTeX tailoring.

Start SwitchMaxer locally with `python -m switchmaxer.main`, verify that
http://127.0.0.1:8765 responds, and tell me how to open and stop it. If a different
service is already on that port, leave it running and report the conflict.
Limit this request to setup and verification. Leave profile information, resumes,
job discovery, and applications for a separate request from me.
```

Assistants with skill support can load the [optional setup skill](skills/switchmaxer-setup/SKILL.md) from the checkout for profile and source configuration.

## Run locally

Use Python 3.11 or newer. SwitchMaxer has been tested on macOS.

### macOS and Linux

```sh
git clone https://github.com/Yash-Garg-21/switchmaxer.git
cd switchmaxer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m switchmaxer.main
```

### Windows PowerShell

```powershell
git clone https://github.com/Yash-Garg-21/switchmaxer.git
Set-Location switchmaxer
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m switchmaxer.main
```

Open [SwitchMaxer at 127.0.0.1:8765](http://127.0.0.1:8765). The server listens on the loopback address. Press `Ctrl+C` in the terminal running it to stop it.

## Use the dashboard

1. **Profile.** Upload one or more PDFs, choose a default, and save your job preferences and reusable answers. The default resume can suggest your application email. Notice defaults to 60 days.
2. **Sources.** Add public Greenhouse, Lever, Ashby, or SmartRecruiters job boards by hosted board URL or slug. SwitchMaxer can also crawl career pages where permitted and read structured job listings. Import LinkedIn, Naukri, and Instahyre jobs through downloaded alert emails, pasted alerts, or posting links. An employer apply URL or requisition ID helps merge duplicate listings.
3. **Jobs.** Refresh the sources and shortlist roles. Open a role to see its description, experience requirements, and recognized skills. Use the applied checkbox to track applications you sent elsewhere.
4. **Review queue.** Prepare the most relevant resume for each job. Review the PDF, any skills changes, and saved form answers before approving selected jobs as a batch.
5. **Applications.** Open the live employer form with your approved PDF and answers ready. Complete the form, sign in or enter any verification codes directly in the portal, and mark it submitted after confirmation. Automated submission runs against the local test portal.

Download `jobs.html` and `applications.html` from the dashboard. `jobs.html` lists discoveries and their applied status. `applications.html` records confirmed submissions, the original resume filename, and any skill changes. It labels local test submissions. Both standalone files are also saved under `exports` in the data directory.

### SmartRecruiters boards

Select **SmartRecruiters** in **Sources** and enter a hosted board URL such as `https://careers.smartrecruiters.com/example?country=in&q=backend`. The `country` filter and `q` search term are both optional. SwitchMaxer uses the public GET endpoints to list postings and fetches full job descriptions for a limited number per refresh. [SmartRecruiters Posting API](https://developers.smartrecruiters.com/docs/endpoints)

### Optional LaTeX tailoring

PDF uploads work on their own. For skills-only tailoring, install [Tectonic](https://tectonic-typesetting.github.io/), enable tailoring in **Profile**, confirm the skills you can claim, and attach the matching local LaTeX project. A typical Russell-style layout uses `resume.tex` and `content/skills.tex`.

Compile the original project once to prime the Tectonic cache. The first build may download its TeX bundle. On macOS or Linux, run this from the LaTeX project folder to keep build outputs separate.

```sh
tectonic --untrusted --outdir "$(mktemp -d)" resume.tex
```

SwitchMaxer edits a temporary copy of the skills section and validates the resulting PDF. If validation fails, the original PDF is available for review.

### Test batch submission

In **Sources**, choose **Load test jobs**. With a PDF available in **Profile**, shortlist the three fixtures, prepare their resumes, and approve the batch. The portal confirms one submission, pauses one for verification, and times out on one after recording a receipt. Use **Applications** to complete the test challenge and check the receipt. This flow stays local.

## Local data and setup CLI

SQLite, uploaded PDFs, generated PDFs, and HTML exports stay in the app data directory. Discovery fetches public listings from configured sources. The app uses your local browser for live application handoff and keeps passwords and one-time codes out of its workflow.

| Platform | Default data directory |
| --- | --- |
| macOS | `~/Library/Application Support/SwitchMaxer` |
| Linux | `$XDG_DATA_HOME/switchmaxer` or `~/.local/share/switchmaxer` |
| Windows | `%LOCALAPPDATA%\SwitchMaxer` |

Set `SWITCHMAXER_DATA_DIR` to override the default. The older `JOBAPPLY_DATA_DIR` variable is also recognized as a fallback. On macOS, SwitchMaxer reuses an existing `JobApply` data directory by default.

Use the setup CLI to inspect saved settings or merge a JSON configuration from standard input. Pass `--data-dir` before `inspect` or `configure` to work with a specific data directory.

```sh
.venv/bin/python -m switchmaxer.setup inspect
printf '%s\n' '{"profile":{"target_titles":"Software Engineer"}}' | .venv/bin/python -m switchmaxer.setup configure
```

In PowerShell, use the virtual environment's Windows executable and pipe a JSON string to `configure`.

```powershell
.\.venv\Scripts\python.exe -m switchmaxer.setup inspect
'{"profile":{"target_titles":"Software Engineer"}}' | .\.venv\Scripts\python.exe -m switchmaxer.setup configure
```

The [setup skill](skills/switchmaxer-setup/SKILL.md) documents the accepted JSON sections.

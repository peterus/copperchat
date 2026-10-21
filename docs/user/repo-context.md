# Repository context

The agent can **read** the repository your KiCad project lives in. So it can answer
questions such as "What is this project for?", "Is there an enclosure, and how big is
it?" or "What does the firmware expect on this pin?" from your README, your Markdown
specs, an OpenSCAD enclosure (`.scad`) or the firmware sources, instead of guessing.

Access is **read-only**. The agent cannot write, rename or delete anything in the
repository through these tools; its write paths are unchanged (design docs under
`docs/design/`, schematic edits through KiCad, the part researcher's notes under
`docs/parts/`). Every session type gets the repository tools.

## Which repository

The **repository root** is the boundary for everything below:

- If the project folder is in a git repository, the root is the git top level, provided
  it contains the project folder and lies **strictly inside** the projects folder
  (`/projects`). A `/projects` folder that is itself a git repository therefore never
  exposes your other projects.
- Otherwise the root is the project folder (the one with the `.kicad_pro`).

Example: in a repository `OpenDriveHub` with the KiCad project in
`hardware/transmitter/pcb/main/`, the agent sees the README at the top, the enclosure in
`hardware/transmitter/parts/*.scad` and the firmware next to it. Other KiCad projects in
the same repository are visible too.

## The tools

All paths are relative to the repository root, with forward slashes.

| Tool | What it does |
|---|---|
| `repo_list` | Lists the files in a folder, plus its sub-folders with file counts. With a glob (`**/*.scad`) it lists every matching file below the folder. Each file is marked `text`, `binary`, `kicad`, `large` (over 1 MiB) or `unknown`. |
| `repo_grep` | Searches the text files for a string (literal and case-insensitive by default, or a Python regex). Optional folder and file filter (`*.scad`). Returns path, line number and the line. |
| `repo_read` | Reads a text file, at most 1000 lines and 64 KiB per call. Longer files are read in pages (`offset`). |

The chat shows these calls like any other tool call.

## The project brief

At the **first turn of every session** the agent gets a short brief of the repository,
just before the design doc index:

- the repository name and where the KiCad project sits in it,
- an excerpt of the README at the root (`README.md`, `README` or `README.txt`, any case),
  plus the project folder's own README if the project is not at the root (together at
  most 3 KiB, cut at a line end),
- a folder tree two levels deep, with file counts and the three most common file types
  per folder; if two levels need more than 40 lines it shows one level, cut after 40
  lines with "+N more folders".

The whole brief is at most 6 KiB; if needed the tree drops to one level and then the
README excerpt gets shorter. The README text is passed as quoted project data, like the
design docs: it never counts as an instruction. A README that says "commit" does not let
the agent commit; only your own message can.

- A session created before this feature gets the brief once, at its next turn.
- **Reopening** a session does not send it again. A **handover** (close, then a new
  session) does, because the new session starts fresh.
- If the brief cannot be built, the agent gets one line instead ("Project repository
  brief unavailable: <reason>; use repo_list") and the session goes on.
- Later turns get no brief and no notice of changed repository files. The agent reads
  the current state with the tools when it needs it.

## What the agent cannot see

**Blocked paths** are hidden from listings and searches and are never readable. Each
rule is checked, ignoring case, against every folder and file name in the path, so a
blocked folder hides everything below it.

| Kind | Names |
|---|---|
| Git internals | `.git` (folder or file), plus the repository's own git folder under any other name (for example a bare-repo layout) and any folder shaped like a git folder |
| Environment files | `.env*` (`.env`, `.env.local`, …), `*.env` |
| Keys and credentials | `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keystore`, `*.jks`, `.netrc`, `.npmrc`, `.pypirc`, `*credentials*`, `*secret*`, `*.kdbx` |
| Credential stores | `.ssh`, `.gnupg`, `.aws`, `.pgpass`, `.htpasswd` |
| Terraform state | `*.tfstate`, `*.tfstate.*`, `*.tfvars` |
| CopperChat state | `.kicad-agent` |
| KiCad backups, caches, locks | `*-backups`, `fp-info-cache`, `*.lck` |

**Listed, but not readable or searchable:**

- **KiCad files** (`*.kicad_sch`, `*.kicad_pcb`, `*.kicad_sym`, `*.kicad_mod`,
  `*.kicad_pro`, `*.kicad_prl`, `*.kicad_dru`, `*.kicad_wks`, `sym-lib-table`,
  `fp-lib-table`). The agent reads the design only through the KiCad tools.
- **Binary files** (a NUL byte or invalid UTF-8 in the first 8 KiB): PDFs, images, STL,
  STEP and so on. The agent knows they exist but cannot open them.
- **Files over 1 MiB** (marked `large`): use `repo_grep` to find the relevant lines.

**Also refused:**

- In a git repository, files ignored by `.gitignore` are neither listed nor readable,
  not even by their exact path.
- The contents of git submodules.
- Absolute paths, `~`, `..`, backslashes, control characters and paths over 400
  characters.
- Symlinks that leave the repository root or point at a blocked path. A symlink that
  stays inside the root is followed for reading; listings and searches never descend
  into symlinked folders.

## Limits

| Tool | Limit |
|---|---|
| `repo_list` | 200 entries by default, at most 500 per call. The file list behind it stops at 20 000 files (the result says `truncated`) and is cached for 10 s. |
| `repo_grep` | Query 1–200 characters; 50 results by default, at most 100. A search stops at the result limit, after 8 MiB scanned or after 5 s, and says which limit it hit. It runs in its own process, which is killed at the time limit, so a runaway regex cannot hang the app. Lines are cut to 300 characters. |
| `repo_read` | At most 1000 lines and 64 KiB per call (continue at `next_offset`); files over 1 MiB are refused. |

## How it is enforced

The same path rules are checked three times: by the permission check the agent SDK asks
before every tool call, by a pre-tool hook that sees every call, and again inside the
tool itself. The agent still has no built-in file tools (no Read, Glob, Grep, shell or
web).

The **part researcher** is unchanged: it keeps its own rules (reading `docs/parts/` and
your uploads, including PDFs). The block list above does not apply to it.

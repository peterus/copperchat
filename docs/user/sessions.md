# Sessions and design docs

From v0.3.0 a project has several chat **sessions**, and the agent works from **design
docs** under `docs/design/` in the project. A typical flow:

1. A **Requirements** session works out with you what the board must do and writes it into
   `requirements.md`, `architecture.md`, `parts.md` and `decisions.md`.
2. You read the docs in the **Docs panel**, edit them if needed, and **approve**
   `requirements.md`.
3. You **close** the requirements session. The agent brings the docs up to date and writes
   a short handover.
4. A **Schematic** session starts from the docs, not from the earlier chat, and draws the
   schematic.

## Sessions

### Types

You pick the type when you create a session. It cannot be changed later.

| Type | What the agent can do |
|---|---|
| **Requirements** | Write the design docs, research parts, look up library parts, and read the schematic and board. It cannot change the schematic. |
| **Schematic** | Every schematic tool. In the docs it may only append decisions and update `Open points` (any doc) and `Key parts` (`parts.md`). |
| **Free** | Everything: all schematic tools and all doc tools (like the single chat before v0.3.0). |

Board sessions are shown greyed out. They come with board editing.

The new-session dialog suggests **Requirements** while `docs/design/requirements.md` does
not exist, and **Schematic** after that. Each type loads its own skills. A skill's
`sessions:` front-matter key limits it to some types. The Skills page shows this as type
chips, and you can toggle them on your own (user) skills.

### Switching and running

- The **session header** at the top of the chat shows the type, the title (click to
  rename) and the status. The **▾** opens the session list: newest activity first, with a
  running dot, an unread dot and the cost. Rows can be renamed and archived; "Show
  archived" brings archived sessions back.
- Each session has its own URL (`/p/<project>/s/<session>`). Push notifications open the
  session they are about.
- **One run per project at a time.** While one session runs, another session's messages
  wait in its own queue and run after it, in order. A "Session N running" chip with
  **Stop** shows which session is busy. Stop only stops that session and cancels only its
  own queued messages.
- Each session keeps its own composer draft.

### Close and reopen

- **Close session** (header or list) asks the agent, in one last turn, to bring the design
  docs up to date: record decisions, update `Open points`, and reply with a short handover
  summary. The server stores that summary as a `Session N closed` entry in `decisions.md`.
- A closed session is read-only. **Reopen session** makes it writable again and resumes
  the same agent session.
- Closing is refused while the session owns an open plan: finish or abort the plan
  first.
- If the close turn is stopped or fails, the session goes back to open ("Close did not
  complete"). After a rate limit it closes once the limit is over.

### What a new session knows

A new session does not see the chat of earlier sessions. At its first turn it gets an
index of the design docs: status, open points and the latest decisions. A Schematic
session also gets the text of `requirements.md`, `architecture.md` and `parts.md`. When a
doc changes later (by you or another session), the next turn gets a short notice of what
changed. Doc text is always passed as quoted project data, never as instructions.

## Design docs

The docs live in the project under `docs/design/`, so they are committed with it:

| File | Content |
|---|---|
| `requirements.md` | function, interfaces, power, environment, mechanical, constraints, acceptance criteria, open points |
| `architecture.md` | block tree, power tree, sheet split, interfaces between blocks |
| `parts.md` | key parts table (role, MPN, rationale, status), links to `docs/parts/<MPN>/notes.md` |
| `decisions.md` | append-only log of decisions, one entry per decision |
| `board.md` | reserved for board editing |

You can add more docs as `docs/design/<name>.md` or `docs/design/<dir>/<name>.md`
(at most two levels and 50 files). A missing fixed doc starts from a template. You can
replace a template with your own file in `<data>/skills/design-docs/templates/<name>.md`
(or in the project's `.kicad-agent/skills/design-docs/templates/`). The Skills page does
not edit templates.

### The Docs panel

Open it with the **Docs** button (on a phone: under **More**).

- **Index:** every doc with its status (missing, draft, approved, changed since
  approval), a marker if you edited it, the number of open points, and who changed it
  last, in which session.
- **Doc view:** the rendered doc. Links to `../parts/<MPN>/notes.md` open the part in the
  Parts panel.
- **Edit:** a Markdown editor with preview. The front matter is not shown; the server owns
  it.
- **History:** every change with author, session, summary and +/− lines, a line diff
  against the previous version, and **Revert to this**.
- When the agent changes a doc, the chat shows a card such as "📄 requirements.md updated
  — +12 / −3 (Interfaces, Open points)". **View diff** opens that change.

### Approval

- **Approve** marks the doc's current text as approved by you. Only you can approve, in
  the panel; the agent cannot. A `status: approved` line written into the file by hand
  or by a cloned repository does not count.
- Any later change (yours or the agent's) makes the doc a draft again. If the agent
  changes an approved doc, the chat says "requirements.md changed after approval
  (Session 3) — review and re-approve".
- `decisions.md` has no approval.

### Your edits win

- When you save a doc, it is marked **edited by you**. From then on the agent may no
  longer replace the whole doc. It can still change single sections and append, and each
  change shows up in the history and as a chat card. **Let the agent rewrite** clears the
  mark.
- **Conflicts:** every save carries the version you started from. If the doc changed while
  you were editing, the panel says so instead of saving ("Changed while you were editing,
  by Session 3"). You can look at their change, keep editing on top of it, or discard
  yours. A banner warns you while you edit if the doc changes underneath. The agent gets
  the same check.

## The gate

Creating a **Schematic** session while `requirements.md` is missing or not approved shows
a warning in the dialog: "Requirements are not approved (draft, 2 open points)", with
**Start anyway** and **Open requirements**. If you start anyway, the agent tells you once
that it works from unapproved requirements, then goes on. The gate never blocks a tool.

## Upgrading from v0.2

**Back up `data/` first** (the database moves to a new schema and cannot go back to v0.2):

```bash
docker compose down
cp -a data data.bak-0.2
# set COPPERCHAT_VERSION=0.3.0 in .env (or keep latest), then
docker compose pull && docker compose up -d
```

Each project's existing chat becomes **"Session 1"**, a Free session with the full history.
Its agent session is resumed, so the agent still remembers the earlier conversation. Open
browser tabs of the old version show a banner asking you to reload.

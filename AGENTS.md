# OS1 Local Development

## Git workflow

OS1 is a personal project. Work directly on `main` by default: do not create a
new branch or pull request unless the user explicitly asks for one. When asked
to publish completed work, commit only the files in scope and push `main`
directly. Preserve unrelated local changes.

## Start the project

Run from the repository root:

```bash
.venv/bin/uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>.

Keep the server bound to `127.0.0.1`. OS1 is local-only and is not designed to
be exposed to the LAN or public internet.

If `.venv` does not exist yet:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env` only when local server-side API-key fallbacks are
needed. Never commit `.env` or print its secrets in logs.

## Stop or restart

Stop the foreground server with `Ctrl-C`. Before starting another instance,
check whether port 8000 is already occupied:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

After a computer restart, run the start command again; OS1 is not installed as
a background login service.

## Verify

Check the local health endpoint:

```bash
curl http://127.0.0.1:8000/api/health
```

Run the test suite from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Product and visual language

OS1 should feel quiet, warm, intimate, and precise. The visual reference is the
emotional atmosphere of Samantha's OS in *Her*, not a literal recreation of the
film's interfaces. Preserve the established design language across every new
surface:

- Use the warm OS1 palette: deep ember and coral-red environments, soft sunset
  highlights, and cream rather than pure-white text. Avoid cool corporate blues,
  generic gray dashboards, and unrelated accent colors.
- Favor atmosphere and whitespace over decoration. Use thin low-contrast
  dividers, restrained translucent washes, and gentle depth. Do not turn every
  region into a bordered card or add glass effects without a hierarchy reason.
- Keep typography human and editorial: clear short labels, calm sentence case,
  generous line height, and a small number of deliberate sizes. Large headings
  may be light and expressive; controls remain compact and legible.
- Shapes are soft but not bubbly. The current baseline is roughly 6-10px corner
  radii for controls and surfaces. Avoid excessive pills, heavy shadows, thick
  borders, gradients inside every component, or ornamental icons.
- Motion should be quiet and functional, normally 140-200ms. Prefer small fades,
  opacity changes, and 1-4px movement. Always respect `prefers-reduced-motion`.
- Keep primary actions obvious without making the whole interface loud. Muted
  text must remain readable, keyboard focus must be visible, and layouts must
  work on narrow screens.
- Reuse existing CSS variables, spacing, controls, and interaction patterns when
  working in an established surface. A new page may extend the system, but it
  should still look unmistakably like OS1.

The product itself is intentionally minimal. Show advanced machinery only where
it helps the user understand or control the system. Knowledge management is a
dedicated page at `/knowledge`; the voice conversation remains a focused single
surface. API key settings remain a modal because they are a short, interruptive
task rather than a workspace.

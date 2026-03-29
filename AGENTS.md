# Project AGENTS

This file captures project-specific design context for future Codex sessions working in this repository.

## Local Environment Notes

- Local Python is available at `C:\Users\Kotorin\AppData\Local\Programs\Python\Python312\python.exe`
- Verified working version: `Python 3.12.10`
- When a task needs Python, prefer this absolute path instead of assuming `python` or `py` is available on `PATH`

## Browser Automation Notes

This project was debugged repeatedly with Playwright-style browser scripts. A few failure modes came up often enough that they should be treated as local rules.

### Dependency reality
- The repository currently does **not** include a local Playwright dependency in `frontend/package.json` or a checked-in `playwright` / `playwright-core` package under project `node_modules`.
- Existing artifacts under `backend/data/ui-probe/` are still useful reference material, but they should not be taken as proof that the repo itself can run Playwright scripts locally without additional tooling.
- Before proposing a new in-repo Playwright script, first verify whether Playwright is actually available in the current environment instead of assuming it from the presence of prior screenshots or JSON probes.
- This machine does have a usable Playwright executable at `C:\Users\Kotorin\AppData\Local\Programs\Python\Python312\Scripts\playwright.exe`.
- If later browser automation is needed, prefer this local executable path as the first option instead of assuming a repo-local Node dependency.

### What caused trouble
- Text-based selectors using Chinese UI copy were sometimes unstable when passed through ad-hoc Node stdin scripts on Windows. In logs they could even appear as garbled `????`, which made failures harder to interpret.
- Guessing trigger selectors from memory was unreliable. For example, the top-right tools trigger is `.settings-trigger`, not a generic `.toolbar-menu-button`.
- For alignment bugs, screenshots alone were not enough. Some cases looked like “visual drift”, but the real issue only became obvious after reading DOM rects, computed styles, and element center lines.
- Ant Design drawer/header structure matters. Styling a nested element as if it were absolutely positioned while it is still `position: static` can create misleading layout results that only show up after measuring the live DOM.

### Stable solutions
- Prefer structural selectors or verified class names over raw Chinese text selectors when driving the UI from scripts. First inspect the rendered markup in the repo or DOM, then target the exact class or role.
- When a selector fails, verify the actual trigger/control in code before retrying the browser script. Do not keep guessing selector names.
- For layout verification, capture both:
  - a screenshot under `backend/data/ui-probe/`
  - machine-readable measurements such as `getBoundingClientRect()`, computed `position`, `transform`, and element center lines
- For alignment regressions, compare the center Y values of all relevant controls instead of relying only on screenshots.
- Keep browser automation artifacts in `backend/data/ui-probe/` so later sessions can compare before/after states quickly.

### Recommended local pattern
- Open the page with Playwright and interact with verified class selectors such as `.settings-trigger` and overlay-specific selectors such as `.topbar-menu-overlay`.
- After opening the target panel, read `getBoundingClientRect()` and computed styles for the exact elements being debugged.
- If the issue is visual alignment, save a screenshot and log the relevant center-line numbers in the same run.

## Text Encoding Guardrails

This project contains Chinese UI copy. On Windows, source files with Chinese text are at risk of mojibake if they are edited through the wrong shell/codepage path.

### What likely causes the issue
- PowerShell/terminal output can display UTF-8 text with a non-UTF-8 codepage, making healthy files look corrupted.
- More dangerously, shell-based write operations can actually corrupt files if UTF-8 source text is decoded or re-encoded through the system ANSI/GBK path.
- High-risk operations include shell redirection, ad-hoc string replacement from the terminal, or any command that rewrites source files containing Chinese text without explicitly preserving UTF-8.

### Rules to avoid recurrence
- For source files containing Chinese text, prefer `apply_patch` for edits. Do not use shell redirection or shell text replacement to rewrite those files.
- If inspection is needed, prefer reading the file with Python using explicit UTF-8, not terminal display alone.
- If terminal output looks garbled, do not assume the file is broken. First verify the file contents by reading it with Python and `encoding='utf-8'`.
- If a file is suspected to be truly corrupted, stop patching it incrementally. Reconstruct the file from a clean UTF-8 version instead of stacking more partial edits on top.
- After editing a Chinese source file, run the fastest relevant build check to catch broken strings or malformed syntax immediately.

### Recommended verification pattern
- Read with: `Path(...).read_text(encoding='utf-8')`
- Then run the narrowest build/test command relevant to that file

## Design Context

### Users
This project is a single-user local desktop dashboard used only by the owner of the project.

The user works in a personal workflow centered on:
- refreshing Fanbox posts
- identifying missing local downloads
- downloading archives in batches
- extracting and renaming files
- checking task progress and fixing failures

The interface should optimize for fast scanning, low friction, and a sense of calm control during repeated daily use. It does not need to behave like a mass-market SaaS product or a formal enterprise admin panel.

### Brand Personality
The product should feel:
- light
- refined
- calm

The emotional goal is not excitement or authority. It should feel pleasant, tidy, and quietly capable: a personal tool with polish, not a rigid operations console.

### Aesthetic Direction
The desired visual direction is minimalist and lightweight, with more refinement than the current implementation.

Use a visual language that feels:
- airy rather than dense
- elegant rather than flashy
- soft rather than stern
- structured but not boxy

Anti-direction:
- avoid overly serious enterprise-dashboard styling
- avoid excessive borders, hard dividers, and heavy panel framing
- avoid loud or showy motion

Theme requirements:
- support both light mode and dark mode
- automatically follow the system color scheme
- also provide a manual theme selection option
- include a clear "follow system" theme behavior in the UI

Motion guidance:
- use subtle, graceful motion to make the interface feel lighter and more polished
- prefer understated transitions, hover feedback, and staged reveals
- avoid decorative or high-energy animation

Current implementation already suggests a useful base direction:
- desktop-first local tool
- Ant Design component foundation
- soft warm neutrals with teal accents
- large radius and gentle gradients

Future design work should preserve the useful clarity of the current information architecture while making the page feel more intentional and more delicate.

### Design Principles
1. Prioritize calm efficiency. The UI should help the user understand status and act quickly without visual noise.
2. Reduce dashboard heaviness. Replace rigid admin-panel cues with lighter surfaces, softer separation, and cleaner hierarchy.
3. Make utility feel refined. Keep the product practical, but elevate typography, spacing, color balance, and component rhythm so it feels crafted rather than assembled.
4. Design for repeated personal use. Optimize for the owner's daily workflow, not generic first-time-user onboarding or team collaboration.
5. Let themes feel native. Light and dark modes should both feel intentional, and theme switching should respect system preference while remaining user-controllable.
6. Use motion as polish, not spectacle. Animations should support clarity and elegance, never distract from the tool's operational purpose.

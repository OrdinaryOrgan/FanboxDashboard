# Fanbox Dashboard

Fanbox Dashboard is a local desktop-oriented web app for tracking Fanbox posts, identifying missing local downloads, downloading MEGA archives, extracting them into a year-based library, and applying a custom image renaming workflow.

It is designed for a single-user personal workflow rather than a multi-user SaaS environment. The product focuses on low-friction daily use, quiet visual polish, and clear task visibility.

## What It Does

- Refresh Fanbox posts with a persistent logged-in browser profile
- Detect which posts are missing from the local library
- Download MEGA archives in batches
- Extract downloaded archives into a year-based folder structure
- Rename extracted images according to project-specific rules
- Track task execution, retries, failures, and archive cleanup
- Show the library in a compact dashboard grouped by year

## Core Workflow

The app turns this manual flow:

1. Open Fanbox
2. Check for new posts
3. Open a post
4. Find the MEGA link
5. Download the archive
6. Extract it locally
7. Rename image files

into a managed local workflow:

1. Refresh Fanbox posts
2. Review missing items by year
3. Download one or many posts
4. Reuse an existing local archive if it is already present
5. Extract and rename automatically
6. Track progress and failures from the dashboard

## Architecture

### Backend

- `FastAPI`
- `SQLAlchemy`
- `SQLite`
- `Playwright for Python`
- `MEGAcmd` integration through subprocess execution

The backend is responsible for:

- Fanbox session reuse and post refresh
- Task orchestration
- Download / extract / rename pipelines
- Local library reconciliation
- Serving the frontend build in production-like local usage

### Frontend

- `React`
- `TypeScript`
- `Vite`
- `Ant Design`
- `TanStack Query`

The frontend is responsible for:

- Year-based post browsing
- Theme and privacy-mode UI preferences
- Triggering refresh, download, archive, and task actions
- Displaying task progress and current library status

## Key Design Decisions

### Local-first

This project runs on a local machine and stores state in a local SQLite database. It does not require a remote backend, user accounts, or cloud storage.

### Lightweight persistence

The app uses a single SQLite file instead of a heavier database stack. Runtime data is generated under `backend/data/` and should not be committed.

### Separated remote sync and local scan

The app distinguishes between:

- `Refresh`: remote Fanbox sync
- `Full Sync`: deeper remote scan across all pages
- `Rescan Library`: local filesystem reconciliation

This keeps normal daily refreshes fast while preserving a path for deeper repair and recovery.

### Personal library structure

The local content library is organized by year:

```text
<library_root>/
  2026/
    Archive/
    Post Title A/
    Post Title B/
  2025/
    Archive/
    ...
```

Archives are stored under each year's `Archive/` folder, while extracted content is stored under the year folder using the post title as the directory name.

## Repository Structure

```text
backend/
  app/
    api/          # FastAPI routes
    core/         # config and app-level setup
    db/           # SQLAlchemy models and session
    schemas/      # Pydantic schemas
    services/     # Fanbox / MEGA / files / task orchestration
  tests/          # backend tests

frontend/
  public/
  src/
    assets/
```

Top-level helper scripts:

- `start_dashboard.vbs` - hidden one-click launcher for Windows
- `start_dashboard.cmd` - console launcher
- `start_dashboard.ps1` - PowerShell launcher

## Requirements

### System

- Windows
- Python 3.12+
- Node.js
- `MEGAcmd` installed locally

### Fanbox access

The target Fanbox content requires a logged-in subscribed account. The app uses a dedicated Playwright profile directory instead of reusing the default everyday Edge profile.

## Installation

### 1. Backend dependencies

```powershell
python -m pip install -r backend\requirements-dev.txt
python -m playwright install chromium
```

### 2. Frontend dependencies

```powershell
cd frontend
npm install
```

## Running the App

### One-click local launch

For normal daily usage on Windows:

```text
Double-click start_dashboard.vbs
```

This script:

- starts the backend if needed
- waits for the health check
- opens the dashboard in the browser

### Daily local usage

If a built frontend is available in `frontend/dist`, the backend serves the frontend at:

- [http://127.0.0.1:8000/](http://127.0.0.1:8000/)

### Development mode

Start the backend:

```powershell
$env:PYTHONPATH='backend'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Start the frontend:

```powershell
cd frontend
npm run dev
```

Development URLs:

- Backend: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- Frontend dev server: [http://127.0.0.1:5173/](http://127.0.0.1:5173/)

## Build

Frontend production build:

```powershell
cd frontend
npm run build
```

After the build exists under `frontend/dist`, FastAPI serves it directly.

## Testing

Backend tests:

```powershell
$env:PYTHONPATH='backend'
python -m pytest backend\tests
```

Fast frontend sanity check:

```powershell
cd frontend
npm run build
```

## Runtime Data

Generated local runtime state is stored under:

- `backend/data/`

Typical contents include:

- SQLite database
- browser profile
- temporary task output
- UI automation artifacts during debugging

These files are local-only and are ignored by Git.

## Feature Overview

### Post refresh

- Incremental refresh for daily use
- Full sync for deeper history reconciliation
- Refresh progress reporting
- Duplicate MEGA-link deduplication, keeping the smaller `post_id`

### Download pipeline

- Queue-based downloads
- Configurable download concurrency
- Silent background `MEGAcmd` execution on Windows
- Existing archive reuse to avoid unnecessary redownloads
- Download timeout handling

### File processing

- Automatic extraction of `.zip` archives
- Year-based archive storage
- Automatic image renaming
- Optional auto-delete of archives after successful extraction

### Dashboard UI

- Year-based browsing
- Compact tiled post layout
- Pagination
- Theme switching
- Follow-system theme option
- Privacy mode for hiding cover images

### Task management

- Task history persisted in SQLite
- Retry support
- Clear-task-list backend support
- Interrupted task reconciliation on startup

## Configuration

Most runtime settings are editable inside the dashboard UI, including:

- Fanbox creator/posts URL
- Playwright profile directory
- download temp directory
- library root directory
- MEGAcmd path or command
- browser channel
- refresh interval
- download concurrency
- posts per row
- auto-delete archive after extraction
- follow system theme

## Known Constraints

- Fanbox page structure and API behavior may change over time
- The project assumes a local single-user workflow
- MEGAcmd command behavior depends on the local machine installation
- Some UI preferences are stored in the browser rather than in SQLite
- A valid Fanbox login session is still required for remote refresh

## Notes for Contributors and Agents

- Prefer small, reviewable diffs
- Keep changes aligned with the existing architecture
- Do not commit runtime data, archives, screenshots, build output, or browser profiles
- Be careful when editing Chinese UI text on Windows: preserve UTF-8 and avoid shell-based rewrites for source files
- See [AGENTS.md](./AGENTS.md) for project-specific development guidance

## License

No license file is currently included in this repository. Add one before publishing if you want to define reuse terms explicitly.

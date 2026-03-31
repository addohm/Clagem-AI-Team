# [PROJECT_NAME] — Technical Architecture

> **How to use this template**
> Search for every `[PLACEHOLDER]` and replace it with your project's specifics.
> Fill in the directory tree and code examples to match your actual structure.
> Delete this callout block when done.

## Overview

[PROJECT_NAME] is a [describe the stack at a high level — e.g. "full-stack monorepo with a Django backend and React frontend, deployed via Docker Compose"]. [One or two sentences on the key structural decisions — e.g. "The backend is a single app with all API logic centralized in one file."]

```
[project-root]/
├── [BACKEND_DIR]/                  # [Backend framework] project
│   ├── [config or settings dir]/
│   │   ├── settings/
│   │   │   ├── development.[ext]   # [dev-specific config: local DB, debug flags]
│   │   │   └── production.[ext]    # [prod-specific config: env-driven]
│   │   └── [entry point files]
│   ├── [core app dir]/             # [Main application module]
│   │   ├── models.[ext]            # Data models
│   │   ├── api.[ext]               # API endpoints
│   │   ├── schemas.[ext]           # Request/response schemas
│   │   ├── [other modules]/
│   │   ├── migrations/             # Database migrations
│   │   └── tests/
│   ├── [media or uploads dir]/     # User-uploaded and generated files
│   ├── requirements.txt / pyproject.toml / package.json
│   └── Dockerfile / Dockerfile.prod
├── [FRONTEND_DIR]/                 # [Frontend framework] SPA / SSR app
│   ├── src/
│   │   ├── [App entry]             # Root component: global state, routing
│   │   ├── [config file]           # API base URL, feature flags
│   │   ├── pages/                  # One file per route
│   │   ├── components/             # Shared UI components
│   │   ├── hooks/                  # Custom hooks
│   │   └── [constants or utils]/
│   ├── [build config]              # e.g. vite.config.js, webpack.config.js
│   └── Dockerfile / Dockerfile.prod
├── docker-compose.dev.yml
├── docker-compose.prod.yml
├── CLAUDE.md
└── GEMINI.md
```

---

## Backend Architecture

### API Layer

[Describe the API framework and organizational approach — e.g. "All API logic lives in `[BACKEND_DIR]/[app]/api.[ext]`. There is no router splitting — a single router instance handles every endpoint."]

**Authentication**: [Describe the auth mechanism — e.g. "HMAC-signed tokens / JWT / session-based. Tokens expire after X days. The auth middleware injects the User object into each request."]

**Schema validation**: [Describe how request/response shapes are defined — e.g. "All input/output shapes are Pydantic v2 schemas defined in `schemas.[ext]`."]

**[Any other key API patterns]**: [Document any non-obvious patterns the AI agents must follow — e.g. attaching computed attributes to model instances before serialization, envelope response shapes, pagination conventions.]

```[lang]
# Example of any non-obvious pattern the agents need to follow:
# [paste a representative code snippet here]
```

### Model / Data Conventions

[Document any model-level behaviors agents must not break — e.g. overridden `save()` / `delete()` methods, signal handlers, auto-sync side effects, protected records.]

- **[Model name].save()**: [What it auto-syncs or validates]
- **[Model name].delete()**: [Any guard logic]
- **Protected records**: [Records that must never be deleted and why]

### Background Processing

[Describe any async/background work — e.g. task queues, daemon threads, cron jobs, webhooks.]

```[lang]
# Example background job pattern used in this project:
# [paste representative snippet]
```

### Configuration / Settings Pattern

[Describe how tuneable values are managed — e.g. DB-stored settings table, environment variables, feature flags.]

```[lang]
# Example of how to read a configurable value:
# [paste representative snippet]
```

---

## Frontend Architecture

### State Management

[Describe the state approach — e.g. "No state library. Global user data lives in `App.[ext]` as a useState hook and is passed down via props. Child components trigger a refresh via a custom window event."]

```
[AppRoot] ([globalState] state)
├── [Navbar component] (displays [key global data])
└── [Router]
    ├── [Page A] (receives [props])
    ├── [Page B] (receives [props])
    └── [Page C] (receives [props])
```

### [Key Frontend Subsystem — e.g. Audio Engine, Realtime, Animation]

[Describe any non-trivial frontend subsystem the agents need to know about.]

### [Key Frontend Subsystem — e.g. Data Fetching, Auth Flow, File Uploads]

[Describe another subsystem if relevant.]

### localStorage / sessionStorage Keys

| Key | Purpose |
|-----|---------|
| `[app_prefix]_token` | Bearer auth token |
| `[app_prefix]_[key]` | [Purpose] |
| `[app_prefix]_[key]` | [Purpose] |

---

## Deployment Architecture

### Development Stack
```
Host machine
├── :[FRONTEND_PORT] → [frontend] ([dev server with hot reload])
├── :[BACKEND_PORT]  → [backend]  ([dev server])
└── [Database] (Docker volume or local)
```

### Production Stack
```
Internet → [Reverse proxy / CDN]
              ├── [frontend service] ([static file server / SSR])
              └── [backend service]  ([app server on :[PORT]])
                    └── [db service] ([DB engine], internal network only)
```

[Describe any shared Docker volumes and what they contain:]
- `[volume_name]` — [purpose, e.g. database files]
- `[volume_name]` — [purpose, e.g. user uploads shared between services]
- `[volume_name]` — [purpose, e.g. built static assets]

[Note any build-time environment variables — e.g. "The frontend Dockerfile.prod accepts `VITE_API_BASE_URL` as a build arg."]

---

## Security Model

- **Authentication**: [Token type, expiry, refresh strategy if any]
- **Email / identity verification**: [Required or not, how it works]
- **Authorization**: [How ownership and role checks are enforced — e.g. "Deck mutation endpoints filter by `user=request.auth` to enforce ownership."]
- **Staff / admin endpoints**: [How privileged routes are gated]
- **Protected data**: [Any records or files that have special delete guards]
- **[Other security concern specific to your project]**: [e.g. rate limiting, CORS policy, file upload sanitization]

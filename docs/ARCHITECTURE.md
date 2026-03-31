# FlashQuest — Technical Architecture

## Overview

FlashQuest is a full-stack monorepo with a Django backend and React frontend, deployed via Docker Compose. The application is structured as a single Django app (`core`) with all API logic centralized in one file.

```
flashquest/
├── backend/                    # Django project
│   ├── config/
│   │   ├── settings/
│   │   │   ├── development.py  # SQLite, console email, CORS localhost
│   │   │   └── production.py   # PostgreSQL, SMTP, env-driven config
│   │   ├── urls.py             # Mounts /admin/ and /api/
│   │   └── asgi.py / wsgi.py
│   ├── core/                   # Single Django app
│   │   ├── models.py           # All models
│   │   ├── api.py              # All API endpoints (Django Ninja)
│   │   ├── schemas.py          # All Ninja input/output schemas
│   │   ├── admin.py            # Django admin with bulk upload
│   │   ├── tts.py              # EdgeTTS audio generation
│   │   ├── constants.py        # SUPPORTED_VOICES, LEAGUE_HIERARCHY
│   │   ├── management/
│   │   │   └── commands/
│   │   │       ├── init_overworld.py   # DB seeding (run once after migrate)
│   │   │       ├── league_reset.py     # Weekly league cycle
│   │   │       ├── cleanup_avatar_config.py
│   │   │       └── migrate_card_audio_paths.py
│   │   ├── migrations/         # 36+ migration files
│   │   └── tests/
│   │       ├── test_league_reset.py
│   │       └── test_streak_repair.py
│   ├── media/                  # User-uploaded and generated files
│   │   ├── cards/
│   │   │   ├── audio/          # EdgeTTS MP3 files (cached by MD5 hash)
│   │   │   └── images/         # Card images (max 512px)
│   │   └── store/
│   │       ├── bases/          # Avatar base character images
│   │       ├── backgrounds/    # Scene background images
│   │       └── items/          # Categorized item images
│   │           ├── clothes/
│   │           ├── head/
│   │           ├── sneakers/
│   │           ├── pets/
│   │           └── jewelry/
│   ├── requirements.txt
│   └── Dockerfile / Dockerfile.prod
├── frontend/                   # React + Vite SPA
│   ├── src/
│   │   ├── App.jsx             # Root: global state, nav, audio engine
│   │   ├── config.js           # API_BASE_URL resolution
│   │   ├── main.jsx            # React entry point
│   │   ├── pages/              # One file per route
│   │   ├── components/         # Shared components
│   │   ├── hooks/
│   │   │   └── useArcadeSounds.js
│   │   ├── constants/
│   │   │   └── languages.js
│   │   └── fonts/              # PixelifySans, Tiny5
│   ├── package.json
│   ├── vite.config.js
│   └── Dockerfile / Dockerfile.prod
├── docker-compose.dev.yml
├── docker-compose.prod.yml
├── CLAUDE.md
└── claude_docs/
```

---

## Backend Architecture

### API Layer (Django Ninja)

All API logic lives in `backend/core/api.py`. There is no router splitting — a single `NinjaAPI()` instance handles every endpoint.

**Authentication**: `TimestampSigner` creates tokens by signing the user ID. Tokens expire after 30 days. Email verification is required before any authenticated action succeeds. The `AuthBearer` class implements `HttpBearer` and injects the `User` object into `request.auth`.

**Schema validation**: All request/response shapes are defined in `core/schemas.py` as `ninja.Schema` classes (Pydantic v2).

**Data attachment pattern**: Django Ninja cannot serialize computed attributes that aren't model fields. The pattern used throughout is to attach them directly to the model instance before returning:
```python
deck.new_count = Card.objects.filter(...).count()
deck.due_count = UserCardProgress.objects.filter(...).count()
return deck  # Ninja schema picks up the attached attrs
```

### Model Sync Protocols

`StoreItem.save()` auto-syncs to `AvatarBase` when `category.slug == 'base'`. This ensures every base item has a corresponding chassis entry for avatar rendering without manual admin steps.

`StoreItem.delete()` is overridden to also clean up the linked `AvatarBase` entry. The `service_streak_repair` item is protected from deletion at both the model level and in Django admin.

### Background Threading

TTS audio generation is offloaded to daemon threads to avoid blocking API responses:
```python
threading.Thread(target=background_audio_gen, args=(...)).start()
```
Audio files are cached by MD5 hash of the text string. A file smaller than 100 bytes is treated as corrupt and regenerated.

### Game Settings Pattern

All tuneable economy values are stored in `GameSetting` (key/value DB table) and retrieved via:
```python
def get_game_setting(key, default):
    setting = GameSetting.objects.filter(key=key).first()
    val = setting.value if setting else str(default)
    if isinstance(default, int): return int(val)
    if isinstance(default, float): return float(val)
    return val
```
This allows live adjustment via Django admin without a deployment.

---

## Frontend Architecture

### State Management

There is no state management library. Global user data (`userStats`) lives in `App.jsx` as a `useState` hook and is passed down via props. Child components that need to trigger a refresh dispatch a `fq_profile_updated` window event, which App.jsx catches and re-fetches `/api/users/me`.

```
App.jsx (userStats state)
├── Navbar (displays coins, level, league, streak)
└── Routes
    ├── Dashboard (receives userStats)
    ├── Study (receives userStats, setUserStats)
    ├── Arcade games (receive userStats, setUserStats)
    └── ... (other pages receive what they need)
```

### Audio Engine

`useArcadeSounds.js` is a custom hook wrapping `use-sound`. It reads volume and mute state from localStorage. The hook is called in every component that needs sounds.

The global audio engine in `App.jsx` attaches document-level `mouseover` and `mousedown` listeners that auto-play hover/click sounds on all interactive elements. Elements that handle their own sound (Study page HIT/MISS buttons) use the class `mute-global-click` to suppress the global click sound.

### Avatar Rendering

`AvatarPreview.jsx` takes an `avatar_config` object and renders layers by iterating over `equipped` entries sorted by the `z_index` stored in `ItemCategory`. The position/scale/mirror values in `avatar_config.position` are applied as CSS transforms.

### localStorage Keys

| Key | Purpose |
|-----|---------|
| `fq_token` | Bearer auth token |
| `fq_volume` | Audio volume (0.0–1.0) |
| `fq_muted` | Mute state ('true'/'false') |
| `fq_extended_dash` | Show extended dashboard widgets |
| `fq_last_level_{userId}` | Level-up detection |
| `fq_repair_shown_{userId}_{timestamp}` | Prevent repeat streak-repair popups |

---

## Deployment Architecture

### Development Stack
```
Host machine
├── :5173 → frontend (Vite dev server, hot reload)
├── :8000 → backend (Django runserver)
└── PostgreSQL (Docker volume)
```

### Production Stack
```
Internet → Nginx Proxy Manager (npm-network)
              ├── fq_frontend (Nginx serving built React + static/media)
              └── fq_backend (Gunicorn on :8000)
                    └── fq_db (PostgreSQL 17, internal network only)
```

Production uses three named Docker volumes:
- `postgres_data` — database files
- `media_data` — user uploads and generated files (shared: backend writes, frontend reads)
- `static_data` — Django collected static files (shared: backend collects, frontend serves)

The frontend `Dockerfile.prod` runs `vite build` at image build time, accepting `VITE_API_BASE_URL` as a build arg. In production this is typically empty (relative paths), since Nginx serves both frontend and proxies `/api/` to the backend.

---

## Security Model

- **Authentication**: Signed tokens (HMAC) with 30-day expiry. No refresh token mechanism.
- **Email verification**: Required before login succeeds. UUID token stored on User.
- **Staff-only endpoints**: Check `request.auth.is_staff` inside the endpoint, return 403 via `HttpError`.
- **Ownership enforcement**: Deck/card mutation endpoints use `get_object_or_404(Deck, id=id, user=request.auth)` to ensure ownership.
- **Protected items**: `service_streak_repair` StoreItem cannot be deleted via admin or model `delete()`.
- **Chroma-key tool**: Artificer (`/artificer` route) is only accessible by staff users; all admin API endpoints verify `is_staff`.

# The Journal — Independent Physics Platform
## Deployment Guide

### What's in this package
- `server.py` — Complete Flask backend (API + PWA serving)
- `index.html` — PWA front-end entry point  
- `manifest.json` — PWA manifest (app name, icons, theme)
- `sw.js` — Service worker (offline caching)
- `static/` — App icons
- `Procfile` — Heroku/Railway process file
- `render.yaml` — Render.com one-click deploy config
- `requirements.txt` — Python dependencies

### Deploy to Render (FREE — recommended)

1. Create a free account at https://render.com
2. Push this folder to a GitHub repo
3. In Render dashboard: New → Web Service → Connect your repo
4. Render auto-detects `render.yaml` and deploys
5. Your app is live at `https://your-app.onrender.com`

### Deploy to Railway (FREE tier)

1. Install Railway CLI: `npm install -g @railway/cli`
2. `cd` into this folder
3. `railway login`
4. `railway init`
5. `railway up`
6. Your app is live at the URL Railway gives you

### Add to Home Screen (PWA)

Once deployed:
1. Open the URL on your phone in Safari (iOS) or Chrome (Android)
2. **iOS**: Tap Share → "Add to Home Screen"
3. **Android**: Tap menu → "Add to Home Screen" or "Install App"
4. The app icon appears on your home screen and runs full-screen

### First Login

Admin account (change password immediately):
- Email: `admin@journal.local`
- Password: `change-me-now`

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/auth/register | Create account |
| POST | /api/auth/login | Sign in |
| GET | /api/auth/me | Current user |
| GET | /api/categories | List categories |
| GET | /api/feed/discovery | Discovery feed |
| GET | /api/feed/published | Published archive |
| POST | /api/submissions | Submit paper (auto desk review) |
| GET | /api/submissions/{id} | Paper detail + comments |
| POST | /api/submissions/{id}/like | Toggle like |
| POST | /api/submissions/{id}/bookmark | Toggle bookmark |
| POST | /api/submissions/{id}/comments | Add comment |
| GET | /api/users/{id} | User profile |
| GET | /api/notifications | User notifications |
| GET | /api/stats | Platform stats |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| SECRET_KEY | dev-change... | Flask session key |
| DATABASE_URL | sqlite:///instance/journal.db | Database URI |
| PORT | 5000 | Server port |

All Rights Reserved.

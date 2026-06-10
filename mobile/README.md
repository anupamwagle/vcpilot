# VCPilot Mobile App

React Native (Expo) mobile trading interface for VCPilot.

## Prerequisites

- Node.js 18+
- Expo CLI: `npm install -g expo-cli` (or use `npx expo`)
- Expo Go app on your phone (iOS or Android) — for dev testing
- Your VCPilot server running and accessible on your local network

## Quick Start

```bash
cd mobile
npm install
npx expo start
```

Scan the QR code with Expo Go on your phone.

## Connecting to Your Server

When you log in, tap **Advanced → Server settings** and enter your server URL:

| Scenario | URL |
|---|---|
| Local network (same Wi-Fi) | `http://192.168.1.X:8501` (use your server's IP) |
| Cloudflare tunnel | `https://yourapp.your-domain.com` |

Find your server's local IP:
- Windows: `ipconfig` → IPv4 Address
- The port is `8501` (VCPilot API default)

## Login Credentials

Use your VCPilot dashboard credentials (email + password).  
The mobile app uses JWT — your token is stored securely in the device keychain (Expo SecureStore).

## What's in the App

| Tab | What it shows |
|---|---|
| **Dashboard** | Live P&L, positions count, signals count, market regime, worker status. Auto-refreshes every 30s. |
| **Positions** | All open positions with live unrealised P&L. Tap **Close ↗** to close with full Minervini exit reason picker. |
| **Signals** | Today's breakout signals (configurable 1/7/14 day window). Filter by status. Skip/restore signals. Watchlist tab shows stocks approaching VCP completion. |
| **Actions** | Pause/resume trading, force screener, refresh price data, evaluate regime, ping worker, send WhatsApp report. Trade history with win rate stats. |

## Building for Production

### iOS (requires macOS + Xcode)
```bash
npx expo build:ios
# or with EAS Build (recommended):
npm install -g eas-cli
eas build --platform ios
```

### Android
```bash
eas build --platform android
```

### Using EAS Build (no local Xcode/Android Studio needed)
```bash
npm install -g eas-cli
eas login
eas build:configure
eas build --platform all
```

## Architecture Notes

- **Auth**: JWT stored in Expo SecureStore (device keychain). 7-day expiry.
- **Data**: TanStack Query handles caching + background refetch. Dashboard refreshes every 30s, positions every 15s.
- **Push notifications**: Expo Notifications. The backend sends pushes when orders fill (requires additional backend wiring for production).
- **Theme**: Dark charcoal/emerald palette matching the VCPilot web dashboard.

## File Structure

```
mobile/
├── App.tsx                     Root — QueryClient + AuthProvider + Navigator
├── src/
│   ├── api/client.ts           Axios instance + all API calls
│   ├── contexts/AuthContext.tsx JWT auth state
│   ├── navigation/AppNavigator.tsx Bottom tab navigator
│   ├── screens/
│   │   ├── LoginScreen.tsx
│   │   ├── DashboardScreen.tsx
│   │   ├── PositionsScreen.tsx
│   │   ├── SignalsScreen.tsx    (Signals + Watchlist tabs)
│   │   └── ActionsScreen.tsx   (Quick actions + Trade history)
│   ├── components/
│   │   ├── StatCard.tsx
│   │   ├── RegimeBadge.tsx
│   │   ├── PositionCard.tsx    (with full close modal)
│   │   └── SignalCard.tsx      (with skip/restore)
│   ├── theme/colors.ts         Dark trading theme
│   └── types/index.ts          TypeScript interfaces
```

## Backend API

The mobile app uses JWT-authenticated endpoints at `/api/mobile/*` on your existing FastAPI server.  
These are registered automatically via `app/api/mobile.py` → `dashboard/main.py`.

Key endpoints:
- `POST /api/mobile/auth/login` — returns JWT
- `GET /api/mobile/dashboard` — home stats
- `GET /api/mobile/positions` — open positions
- `POST /api/mobile/positions/{id}/close` — close a position
- `GET /api/mobile/signals` — signals
- `GET /api/mobile/watchlist` — watchlist
- `POST /api/mobile/actions/*` — all quick actions

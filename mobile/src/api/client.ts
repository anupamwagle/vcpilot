/**
 * VCPilot API client
 * Reads base URL + token from SecureStore.
 * All requests use Bearer JWT auth.
 */
import axios, { AxiosInstance, AxiosRequestConfig } from 'axios';
import * as SecureStore from 'expo-secure-store';

const DEFAULT_BASE_URL = 'http://localhost:8501';

export const STORAGE_KEYS = {
  TOKEN: 'vcpilot_token',
  BASE_URL: 'vcpilot_base_url',
  USER: 'vcpilot_user',
} as const;

let _baseURL = DEFAULT_BASE_URL;
let _token: string | null = null;

/** Call once on app start to hydrate from storage. */
export async function initClient(): Promise<void> {
  const [storedURL, storedToken] = await Promise.all([
    SecureStore.getItemAsync(STORAGE_KEYS.BASE_URL),
    SecureStore.getItemAsync(STORAGE_KEYS.TOKEN),
  ]);
  if (storedURL) _baseURL = storedURL.replace(/\/$/, '');
  if (storedToken) _token = storedToken;
}

export function setToken(token: string | null) {
  _token = token;
  if (token) SecureStore.setItemAsync(STORAGE_KEYS.TOKEN, token);
  else SecureStore.deleteItemAsync(STORAGE_KEYS.TOKEN);
}

export function setBaseURL(url: string) {
  _baseURL = url.replace(/\/$/, '');
  SecureStore.setItemAsync(STORAGE_KEYS.BASE_URL, _baseURL);
}

export function getBaseURL(): string {
  return _baseURL;
}

function createInstance(): AxiosInstance {
  const instance = axios.create({
    timeout: 15000,
    headers: { 'Content-Type': 'application/json' },
  });

  instance.interceptors.request.use((config) => {
    config.baseURL = _baseURL;
    if (_token) config.headers.Authorization = `Bearer ${_token}`;
    return config;
  });

  instance.interceptors.response.use(
    (res) => res,
    (err) => {
      if (err.response?.status === 401) {
        // Token expired — clear it so AuthContext redirects to login
        setToken(null);
      }
      return Promise.reject(err);
    },
  );

  return instance;
}

// Singleton — recreate whenever base URL changes
let _instance = createInstance();
export function getClient(): AxiosInstance {
  return _instance;
}
export function resetClient(): void {
  _instance = createInstance();
}

// ─── Typed API helpers ────────────────────────────────────────────────────────

const PREFIX = '/api/mobile';

export const api = {
  // Auth
  login: (email: string, password: string) =>
    getClient().post(`${PREFIX}/auth/login`, { email, password }),
  me: () => getClient().get(`${PREFIX}/auth/me`),

  // Dashboard
  dashboard: () => getClient().get(`${PREFIX}/dashboard`),

  // Positions
  positions: () => getClient().get(`${PREFIX}/positions`),
  closePosition: (id: number, exit_reason: string, exit_price?: number) =>
    getClient().post(`${PREFIX}/positions/${id}/close`, { exit_reason, exit_price }),

  // Signals
  signals: (days = 7) => getClient().get(`${PREFIX}/signals?days=${days}`),
  skipSignal: (id: number) => getClient().post(`${PREFIX}/signals/${id}/skip`),
  unskipSignal: (id: number) => getClient().post(`${PREFIX}/signals/${id}/unskip`),

  // Watchlist
  watchlist: () => getClient().get(`${PREFIX}/watchlist`),

  // Trades history
  trades: (limit = 30) => getClient().get(`${PREFIX}/trades?limit=${limit}`),

  // Actions
  pause: () => getClient().post(`${PREFIX}/actions/pause`),
  resume: () => getClient().post(`${PREFIX}/actions/resume`),
  forceScreen: () => getClient().post(`${PREFIX}/actions/force-screen`),
  pingWorker: () => getClient().post(`${PREFIX}/actions/ping-worker`),
  refreshData: (exchange_key = 'ASX') =>
    getClient().post(`${PREFIX}/actions/refresh-data?exchange_key=${exchange_key}`),
  evaluateRegime: (exchange_key = 'ASX') =>
    getClient().post(`${PREFIX}/actions/evaluate-regime?exchange_key=${exchange_key}`),
  sendReport: () => getClient().post(`${PREFIX}/actions/send-report`),
};

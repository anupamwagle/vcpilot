import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import * as SecureStore from 'expo-secure-store';
import { api, initClient, setToken, setBaseURL, STORAGE_KEYS } from '../api/client';
import { AuthUser } from '../types';

interface AuthContextType {
  user: AuthUser | null;
  baseURL: string;
  loading: boolean;
  login: (email: string, password: string, serverURL: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [baseURL, setBaseURLState] = useState('http://localhost:8501');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      await initClient();
      const storedUser = await SecureStore.getItemAsync(STORAGE_KEYS.USER);
      const storedURL  = await SecureStore.getItemAsync(STORAGE_KEYS.BASE_URL);
      if (storedURL) setBaseURLState(storedURL);
      if (storedUser) {
        const u: AuthUser = JSON.parse(storedUser);
        setUser(u);
      }
      setLoading(false);
    })();
  }, []);

  const login = async (email: string, password: string, serverURL: string) => {
    setBaseURL(serverURL);
    setBaseURLState(serverURL);
    const { data } = await api.login(email, password);
    setToken(data.access_token);
    setUser(data);
    await SecureStore.setItemAsync(STORAGE_KEYS.USER, JSON.stringify(data));
  };

  const logout = () => {
    setToken(null);
    setUser(null);
    SecureStore.deleteItemAsync(STORAGE_KEYS.USER);
  };

  return (
    <AuthContext.Provider value={{ user, baseURL, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}

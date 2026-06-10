import React, { useCallback } from 'react';
import {
  View, Text, StyleSheet, ScrollView, RefreshControl,
  TouchableOpacity, ActivityIndicator,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { useQuery } from '@tanstack/react-query';
import { C, pnlColor } from '../theme/colors';
import { RegimeBadge } from '../components/RegimeBadge';
import { StatCard } from '../components/StatCard';
import { useAuth } from '../contexts/AuthContext';
import { api } from '../api/client';
import { DashboardData } from '../types';

export function DashboardScreen() {
  const { user, logout } = useAuth();

  const { data, isLoading, isError, refetch, isFetching } = useQuery<DashboardData>({
    queryKey: ['dashboard'],
    queryFn: () => api.dashboard().then((r) => r.data),
    refetchInterval: 30_000, // auto-refresh every 30s
  });

  const onRefresh = useCallback(() => { refetch(); }, [refetch]);

  if (isLoading) {
    return (
      <View style={styles.centered}>
        <ActivityIndicator size="large" color={C.accent} />
        <Text style={styles.loadingText}>Loading dashboard…</Text>
      </View>
    );
  }

  if (isError || !data) {
    return (
      <View style={styles.centered}>
        <Text style={styles.errorEmoji}>⚠️</Text>
        <Text style={styles.errorText}>Could not load dashboard</Text>
        <TouchableOpacity style={styles.retryBtn} onPress={() => refetch()}>
          <Text style={styles.retryText}>Retry</Text>
        </TouchableOpacity>
      </View>
    );
  }

  const workerColor = data.worker_status === 'online' ? C.pos : data.worker_status === 'starting' ? C.warn : C.neg;
  const exchanges = data.active_exchanges?.split(',').map((e) => e.trim()) || ['ASX'];
  const showCrypto = exchanges.some((e) => e.startsWith('CRYPTO'));
  const showASX = exchanges.includes('ASX');

  return (
    <ScrollView
      style={styles.root}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={isFetching} onRefresh={onRefresh} tintColor={C.accent} />}
    >
      <StatusBar style="light" />

      {/* Header */}
      <View style={styles.header}>
        <View>
          <Text style={styles.greeting}>Good {getGreeting()}, {user?.name?.split(' ')[0] || 'Trader'}</Text>
          <Text style={styles.subtitle}>VCPilot Dashboard</Text>
        </View>
        <TouchableOpacity onPress={logout} style={styles.logoutBtn}>
          <Text style={styles.logoutText}>Sign out</Text>
        </TouchableOpacity>
      </View>

      {/* System status strip */}
      <View style={styles.statusStrip}>
        <View style={[styles.dot, { backgroundColor: workerColor }]} />
        <Text style={[styles.statusText, { color: workerColor }]}>
          Worker {data.worker_status}
        </Text>
        {data.trading_paused && (
          <>
            <Text style={styles.statusSep}>·</Text>
            <Text style={[styles.statusText, { color: C.warn }]}>⏸ Paused</Text>
          </>
        )}
        {data.capital_aud > 0 && (
          <>
            <Text style={styles.statusSep}>·</Text>
            <Text style={styles.statusText}>A${data.capital_aud.toLocaleString()} capital</Text>
          </>
        )}
      </View>

      {/* Regime badges */}
      <View style={styles.regimeRow}>
        {showASX && <RegimeBadge regime={data.regime_asx} label="ASX" />}
        {showCrypto && <RegimeBadge regime={data.regime_crypto} label="Crypto" />}
      </View>

      {/* Unrealised P&L hero */}
      <View style={styles.pnlHero}>
        <Text style={styles.pnlLabel}>Unrealised P&L</Text>
        <Text style={[styles.pnlValue, { color: pnlColor(data.total_unrealised_pnl) }]}>
          {data.total_unrealised_pnl >= 0 ? '+' : ''}${Math.abs(data.total_unrealised_pnl).toFixed(2)}
        </Text>
        <Text style={[styles.pnlPct, { color: pnlColor(data.total_unrealised_pct) }]}>
          {data.total_unrealised_pct >= 0 ? '+' : ''}{data.total_unrealised_pct.toFixed(2)}% average
        </Text>
      </View>

      {/* Stats 2×2 grid */}
      <View style={styles.statsRow}>
        <StatCard
          label="Open Positions"
          value={String(data.open_positions_count)}
          icon="📊"
          sub="currently holding"
        />
        <StatCard
          label="Signals Today"
          value={String(data.pending_signals_count)}
          icon="🎯"
          sub="pending entry"
          valueColor={data.pending_signals_count > 0 ? C.blue : undefined}
        />
      </View>
      <View style={styles.statsRow}>
        <StatCard
          label="Today's Realised"
          value={`${data.todays_realised_pnl >= 0 ? '+' : ''}$${Math.abs(data.todays_realised_pnl).toFixed(0)}`}
          icon="💰"
          sub={`${data.todays_trades_count} trade${data.todays_trades_count !== 1 ? 's' : ''} closed`}
          valueColor={pnlColor(data.todays_realised_pnl)}
        />
        <StatCard
          label="Capital"
          value={`A$${(data.capital_aud / 1000).toFixed(1)}k`}
          icon="🏦"
          sub="working capital"
        />
      </View>

      {/* Last updated */}
      <Text style={styles.updatedAt}>
        Updated {new Date(data.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        {isFetching ? ' · refreshing…' : ''}
      </Text>
    </ScrollView>
  );
}

function getGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'morning';
  if (h < 17) return 'afternoon';
  return 'evening';
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  content: { padding: 16, paddingTop: 56 },
  centered: { flex: 1, backgroundColor: C.bg, justifyContent: 'center', alignItems: 'center', gap: 12 },
  loadingText: { color: C.textMuted },
  errorEmoji: { fontSize: 40 },
  errorText: { color: C.textMuted, fontSize: 16 },
  retryBtn: { backgroundColor: C.surface, borderRadius: 10, paddingHorizontal: 20, paddingVertical: 10 },
  retryText: { color: C.accent },
  header: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 },
  greeting: { fontSize: 20, fontWeight: '700', color: C.text },
  subtitle: { fontSize: 13, color: C.textMuted, marginTop: 2 },
  logoutBtn: { paddingVertical: 6, paddingHorizontal: 10 },
  logoutText: { color: C.textMuted, fontSize: 13 },
  statusStrip: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 14 },
  dot: { width: 7, height: 7, borderRadius: 4 },
  statusText: { fontSize: 13, color: C.textMuted },
  statusSep: { color: C.border, fontSize: 13 },
  regimeRow: { flexDirection: 'row', gap: 8, marginBottom: 20 },
  pnlHero: {
    backgroundColor: C.surface,
    borderRadius: 16,
    padding: 20,
    alignItems: 'center',
    marginBottom: 16,
    borderWidth: 1,
    borderColor: C.border,
  },
  pnlLabel: { fontSize: 12, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 },
  pnlValue: { fontSize: 36, fontWeight: '800', letterSpacing: -1 },
  pnlPct: { fontSize: 15, fontWeight: '500', marginTop: 4 },
  statsRow: { flexDirection: 'row', gap: 10, marginBottom: 10 },
  updatedAt: { textAlign: 'center', color: C.textSubtle, fontSize: 11, marginTop: 16, marginBottom: 8 },
});

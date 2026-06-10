import React, { useState, useCallback } from 'react';
import {
  View, Text, StyleSheet, FlatList, RefreshControl,
  TouchableOpacity, ActivityIndicator,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { C } from '../theme/colors';
import { SignalCard } from '../components/SignalCard';
import { api } from '../api/client';
import { Signal, WatchlistItem } from '../types';

type Tab = 'signals' | 'watchlist';

export function SignalsScreen() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>('signals');
  const [days, setDays] = useState(7);

  const signalsQuery = useQuery<{ signals: Signal[]; count: number }>({
    queryKey: ['signals', days],
    queryFn: () => api.signals(days).then((r) => r.data),
    refetchInterval: 60_000,
  });

  const watchlistQuery = useQuery<{ watchlist: WatchlistItem[]; count: number }>({
    queryKey: ['watchlist'],
    queryFn: () => api.watchlist().then((r) => r.data),
    refetchInterval: 120_000,
  });

  const onSignalUpdated = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['signals'] });
    qc.invalidateQueries({ queryKey: ['dashboard'] });
  }, [qc]);

  const isLoading = tab === 'signals' ? signalsQuery.isLoading : watchlistQuery.isLoading;
  const isFetching = tab === 'signals' ? signalsQuery.isFetching : watchlistQuery.isFetching;
  const onRefresh = tab === 'signals' ? signalsQuery.refetch : watchlistQuery.refetch;

  // Filter tabs for signals
  const [statusFilter, setStatusFilter] = useState<string>('ALL');
  const STATUS_FILTERS = ['ALL', 'PENDING', 'TRIGGERED', 'SKIPPED', 'EXPIRED'];

  const signals = signalsQuery.data?.signals ?? [];
  const filteredSignals = statusFilter === 'ALL'
    ? signals
    : signals.filter((s) => s.status === statusFilter);

  return (
    <View style={styles.root}>
      <StatusBar style="light" />

      {/* Page header */}
      <View style={styles.header}>
        <Text style={styles.title}>Signals & Watchlist</Text>
        <View style={styles.tabRow}>
          {(['signals', 'watchlist'] as Tab[]).map((t) => (
            <TouchableOpacity
              key={t}
              style={[styles.tabBtn, tab === t && styles.tabBtnActive]}
              onPress={() => setTab(t)}
            >
              <Text style={[styles.tabText, tab === t && styles.tabTextActive]}>
                {t === 'signals'
                  ? `Signals (${signalsQuery.data?.count ?? 0})`
                  : `Watchlist (${watchlistQuery.data?.count ?? 0})`}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      </View>

      {/* Signals tab */}
      {tab === 'signals' && (
        <>
          {/* Days picker + status filter */}
          <View style={styles.filterRow}>
            {[1, 7, 14].map((d) => (
              <TouchableOpacity
                key={d}
                style={[styles.dayChip, days === d && styles.dayChipActive]}
                onPress={() => setDays(d)}
              >
                <Text style={[styles.dayChipText, days === d && styles.dayChipTextActive]}>
                  {d}d
                </Text>
              </TouchableOpacity>
            ))}
            <View style={styles.filterSep} />
            {STATUS_FILTERS.map((f) => (
              <TouchableOpacity
                key={f}
                style={[styles.dayChip, statusFilter === f && styles.dayChipActive]}
                onPress={() => setStatusFilter(f)}
              >
                <Text style={[styles.dayChipText, statusFilter === f && styles.dayChipTextActive]}>
                  {f === 'ALL' ? 'All' : f.charAt(0) + f.slice(1).toLowerCase()}
                </Text>
              </TouchableOpacity>
            ))}
          </View>

          {isLoading ? (
            <View style={styles.centered}><ActivityIndicator size="large" color={C.accent} /></View>
          ) : filteredSignals.length === 0 ? (
            <View style={styles.empty}>
              <Text style={styles.emptyEmoji}>🎯</Text>
              <Text style={styles.emptyTitle}>No signals</Text>
              <Text style={styles.emptyText}>
                Signals appear after the screener runs. Try increasing the day range or running a force screen.
              </Text>
            </View>
          ) : (
            <FlatList
              data={filteredSignals}
              keyExtractor={(item) => String(item.id)}
              renderItem={({ item }) => <SignalCard signal={item} onUpdated={onSignalUpdated} />}
              contentContainerStyle={styles.list}
              refreshControl={
                <RefreshControl refreshing={isFetching} onRefresh={onRefresh} tintColor={C.accent} />
              }
              showsVerticalScrollIndicator={false}
            />
          )}
        </>
      )}

      {/* Watchlist tab */}
      {tab === 'watchlist' && (
        <>
          {isLoading ? (
            <View style={styles.centered}><ActivityIndicator size="large" color={C.accent} /></View>
          ) : (watchlistQuery.data?.count ?? 0) === 0 ? (
            <View style={styles.empty}>
              <Text style={styles.emptyEmoji}>👁</Text>
              <Text style={styles.emptyTitle}>Watchlist is empty</Text>
              <Text style={styles.emptyText}>
                Stocks meeting 6+/8 Minervini trend criteria are automatically added here.
              </Text>
            </View>
          ) : (
            <FlatList
              data={watchlistQuery.data?.watchlist ?? []}
              keyExtractor={(item) => String(item.id)}
              renderItem={({ item }) => <WatchlistRow item={item} />}
              contentContainerStyle={styles.list}
              refreshControl={
                <RefreshControl refreshing={isFetching} onRefresh={onRefresh} tintColor={C.accent} />
              }
              showsVerticalScrollIndicator={false}
            />
          )}
        </>
      )}
    </View>
  );
}

function WatchlistRow({ item: w }: { item: WatchlistItem }) {
  const passedRules = Object.values(w.rule_results).filter(Boolean).length;
  const totalRules = Object.keys(w.rule_results).length;

  return (
    <View style={wlStyles.row}>
      <View style={wlStyles.left}>
        <View style={wlStyles.tickerRow}>
          <Text style={wlStyles.ticker}>{w.ticker}</Text>
          {w.label && (
            <View style={[wlStyles.labelBadge, { backgroundColor: (w.label_color || C.accent) + '28', borderColor: (w.label_color || C.accent) + '55' }]}>
              <Text style={[wlStyles.labelText, { color: w.label_color || C.accent }]}>{w.label}</Text>
            </View>
          )}
        </View>
        <Text style={wlStyles.meta}>
          {w.exchange_key.replace('CRYPTO_', '')} · Added {w.added_date} by {w.added_by}
        </Text>
      </View>
      {totalRules > 0 && (
        <View style={wlStyles.score}>
          <Text style={wlStyles.scoreText}>{passedRules}/{totalRules}</Text>
          <Text style={wlStyles.scoreLabel}>rules</Text>
        </View>
      )}
    </View>
  );
}

const wlStyles = StyleSheet.create({
  row: {
    backgroundColor: C.surface,
    borderRadius: 12,
    padding: 14,
    marginBottom: 8,
    borderWidth: 1,
    borderColor: C.border,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  left: { flex: 1 },
  tickerRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 4 },
  ticker: { fontSize: 16, fontWeight: '700', color: C.text },
  labelBadge: { borderRadius: 4, paddingHorizontal: 6, paddingVertical: 2, borderWidth: 1 },
  labelText: { fontSize: 10, fontWeight: '600' },
  meta: { fontSize: 12, color: C.textMuted },
  score: { alignItems: 'center' },
  scoreText: { fontSize: 15, fontWeight: '700', color: C.accent },
  scoreLabel: { fontSize: 10, color: C.textSubtle },
});

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  header: {
    paddingHorizontal: 16,
    paddingTop: 56,
    paddingBottom: 10,
    borderBottomWidth: 1,
    borderBottomColor: C.border,
    backgroundColor: C.bg,
  },
  title: { fontSize: 20, fontWeight: '700', color: C.text, marginBottom: 10 },
  tabRow: { flexDirection: 'row', gap: 8 },
  tabBtn: {
    paddingVertical: 6,
    paddingHorizontal: 14,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: C.border,
    backgroundColor: C.surface,
  },
  tabBtnActive: { backgroundColor: C.accent, borderColor: C.accent },
  tabText: { color: C.textMuted, fontSize: 13, fontWeight: '500' },
  tabTextActive: { color: C.bg, fontWeight: '700' },
  filterRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 12,
    paddingVertical: 8,
    gap: 6,
    flexWrap: 'wrap',
  },
  dayChip: {
    paddingVertical: 4,
    paddingHorizontal: 10,
    borderRadius: 8,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.border,
  },
  dayChipActive: { backgroundColor: C.accentDim, borderColor: C.accent },
  dayChipText: { color: C.textMuted, fontSize: 12 },
  dayChipTextActive: { color: C.accent, fontWeight: '600' },
  filterSep: { width: 1, height: 18, backgroundColor: C.border, marginHorizontal: 4 },
  centered: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  empty: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32, gap: 10 },
  emptyEmoji: { fontSize: 48 },
  emptyTitle: { fontSize: 18, fontWeight: '600', color: C.text },
  emptyText: { color: C.textMuted, textAlign: 'center', lineHeight: 20 },
  list: { padding: 14, paddingBottom: 40 },
});

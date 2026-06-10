import React, { useCallback } from 'react';
import {
  View, Text, StyleSheet, FlatList, RefreshControl,
  ActivityIndicator, TouchableOpacity,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { C, pnlColor } from '../theme/colors';
import { PositionCard } from '../components/PositionCard';
import { api } from '../api/client';
import { Position } from '../types';

interface PositionsResponse {
  positions: Position[];
  count: number;
}

export function PositionsScreen() {
  const qc = useQueryClient();

  const { data, isLoading, isError, refetch, isFetching } = useQuery<PositionsResponse>({
    queryKey: ['positions'],
    queryFn: () => api.positions().then((r) => r.data),
    refetchInterval: 15_000,
  });

  const onClosed = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['positions'] });
    qc.invalidateQueries({ queryKey: ['dashboard'] });
  }, [qc]);

  const totalUnrealised = data?.positions.reduce((s, p) => s + p.unrealised_pnl, 0) ?? 0;

  if (isLoading) {
    return (
      <View style={styles.centered}>
        <ActivityIndicator size="large" color={C.accent} />
      </View>
    );
  }

  if (isError) {
    return (
      <View style={styles.centered}>
        <Text style={styles.emptyText}>⚠️ Could not load positions</Text>
        <TouchableOpacity onPress={() => refetch()} style={styles.retryBtn}>
          <Text style={styles.retryText}>Retry</Text>
        </TouchableOpacity>
      </View>
    );
  }

  return (
    <View style={styles.root}>
      <StatusBar style="light" />

      {/* Summary strip */}
      <View style={styles.summary}>
        <Text style={styles.title}>Open Positions</Text>
        <View style={styles.summaryRight}>
          <Text style={styles.count}>{data?.count ?? 0} open</Text>
          {data && data.count > 0 && (
            <Text style={[styles.totalPnl, { color: pnlColor(totalUnrealised) }]}>
              {totalUnrealised >= 0 ? '+' : ''}${Math.abs(totalUnrealised).toFixed(2)}
            </Text>
          )}
        </View>
      </View>

      {data?.count === 0 ? (
        <View style={styles.empty}>
          <Text style={styles.emptyEmoji}>📭</Text>
          <Text style={styles.emptyTitle}>No open positions</Text>
          <Text style={styles.emptyText}>When signals are triggered and orders fill, your positions will appear here.</Text>
        </View>
      ) : (
        <FlatList
          data={data?.positions ?? []}
          keyExtractor={(item) => String(item.id)}
          renderItem={({ item }) => <PositionCard position={item} onClosed={onClosed} />}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={isFetching} onRefresh={refetch} tintColor={C.accent} />
          }
          showsVerticalScrollIndicator={false}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  centered: { flex: 1, backgroundColor: C.bg, justifyContent: 'center', alignItems: 'center', gap: 10 },
  summary: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingTop: 56,
    paddingBottom: 12,
    backgroundColor: C.bg,
    borderBottomWidth: 1,
    borderBottomColor: C.border,
  },
  title: { fontSize: 20, fontWeight: '700', color: C.text },
  summaryRight: { alignItems: 'flex-end' },
  count: { fontSize: 13, color: C.textMuted },
  totalPnl: { fontSize: 16, fontWeight: '700', marginTop: 2 },
  list: { padding: 14, paddingBottom: 40 },
  empty: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32, gap: 10 },
  emptyEmoji: { fontSize: 48 },
  emptyTitle: { fontSize: 18, fontWeight: '600', color: C.text },
  emptyText: { color: C.textMuted, textAlign: 'center', lineHeight: 20 },
  retryBtn: { backgroundColor: C.surface, borderRadius: 10, paddingHorizontal: 20, paddingVertical: 10, marginTop: 8 },
  retryText: { color: C.accent },
});

import React, { useState } from 'react';
import { View, Text, StyleSheet, TouchableOpacity, ActivityIndicator } from 'react-native';
import * as Haptics from 'expo-haptics';
import { C } from '../theme/colors';
import { Signal } from '../types';
import { api } from '../api/client';

interface Props {
  signal: Signal;
  onUpdated: () => void;
}

const STATUS_STYLE: Record<string, { bg: string; text: string; label: string }> = {
  PENDING:   { bg: C.blue + '22',   text: C.blue,    label: '● Pending' },
  TRIGGERED: { bg: C.pos + '22',    text: C.pos,     label: '✓ Triggered' },
  SKIPPED:   { bg: C.textMuted + '22', text: C.textMuted, label: '⊘ Skipped' },
  EXPIRED:   { bg: C.border,        text: C.textSubtle, label: '⊡ Expired' },
  CANCELLED: { bg: C.border,        text: C.textSubtle, label: '✕ Cancelled' },
};

export function SignalCard({ signal: s, onUpdated }: Props) {
  const [loading, setLoading] = useState(false);
  const st = STATUS_STYLE[s.status] || STATUS_STYLE.EXPIRED;
  const canSkip = s.status === 'PENDING';
  const canUnskip = s.status === 'SKIPPED';

  const toggle = async () => {
    await Haptics.selectionAsync();
    setLoading(true);
    try {
      if (canSkip) await api.skipSignal(s.id);
      else if (canUnskip) await api.unskipSignal(s.id);
      onUpdated();
    } catch (e) {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  const rr = s.pivot_price && s.stop_price && s.target_1
    ? ((s.target_1 - s.pivot_price) / (s.pivot_price - s.stop_price)).toFixed(1)
    : null;

  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <View>
          <View style={styles.tickerRow}>
            <Text style={styles.ticker}>{s.ticker}</Text>
            <Text style={styles.date}>{s.signal_date}</Text>
          </View>
          <View style={styles.badges}>
            <View style={[styles.badge, { backgroundColor: st.bg }]}>
              <Text style={[styles.badgeText, { color: st.text }]}>{st.label}</Text>
            </View>
            {s.asset_type === 'CRYPTO' && (
              <View style={[styles.badge, { backgroundColor: C.warn + '22' }]}>
                <Text style={[styles.badgeText, { color: C.warn }]}>⊙ CRYPTO</Text>
              </View>
            )}
          </View>
        </View>
        {(canSkip || canUnskip) && (
          <TouchableOpacity style={styles.skipBtn} onPress={toggle} disabled={loading}>
            {loading ? (
              <ActivityIndicator size="small" color={C.textMuted} />
            ) : (
              <Text style={styles.skipText}>{canSkip ? 'Skip' : 'Restore'}</Text>
            )}
          </TouchableOpacity>
        )}
      </View>

      {/* Price row */}
      <View style={styles.priceRow}>
        <View style={styles.priceItem}>
          <Text style={styles.priceLabel}>Close</Text>
          <Text style={styles.priceVal}>{s.close_price ? `$${s.close_price.toFixed(4)}` : '—'}</Text>
        </View>
        <View style={styles.priceItem}>
          <Text style={styles.priceLabel}>Pivot</Text>
          <Text style={[styles.priceVal, { color: C.accent }]}>{s.pivot_price ? `$${s.pivot_price.toFixed(4)}` : '—'}</Text>
        </View>
        <View style={styles.priceItem}>
          <Text style={styles.priceLabel}>Stop</Text>
          <Text style={[styles.priceVal, { color: C.neg }]}>{s.stop_price ? `$${s.stop_price.toFixed(4)}` : '—'}</Text>
        </View>
        {rr && (
          <View style={styles.priceItem}>
            <Text style={styles.priceLabel}>R/R</Text>
            <Text style={[styles.priceVal, { color: C.blue }]}>{rr}:1</Text>
          </View>
        )}
      </View>

      {/* Scores row */}
      <View style={styles.scoresRow}>
        {s.rs_rating != null && (
          <View style={styles.scoreChip}>
            <Text style={styles.scoreLabel}>RS</Text>
            <Text style={styles.scoreVal}>{s.rs_rating.toFixed(0)}</Text>
          </View>
        )}
        {s.trend_score != null && (
          <View style={styles.scoreChip}>
            <Text style={styles.scoreLabel}>Trend</Text>
            <Text style={styles.scoreVal}>{s.trend_score}/8</Text>
          </View>
        )}
        {s.vcp_contractions != null && (
          <View style={styles.scoreChip}>
            <Text style={styles.scoreLabel}>VCP</Text>
            <Text style={styles.scoreVal}>{s.vcp_contractions}C</Text>
          </View>
        )}
        {s.suggested_size_aud != null && (
          <View style={styles.scoreChip}>
            <Text style={styles.scoreLabel}>Size</Text>
            <Text style={styles.scoreVal}>${s.suggested_size_aud.toFixed(0)}</Text>
          </View>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: C.surface,
    borderRadius: 14,
    padding: 14,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: C.border,
  },
  headerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: 10,
  },
  tickerRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 4 },
  ticker: { fontSize: 17, fontWeight: '700', color: C.text },
  date: { fontSize: 12, color: C.textMuted },
  badges: { flexDirection: 'row', gap: 6 },
  badge: { borderRadius: 4, paddingHorizontal: 6, paddingVertical: 2 },
  badgeText: { fontSize: 11, fontWeight: '600' },
  skipBtn: { backgroundColor: C.surfaceAlt, borderRadius: 8, paddingHorizontal: 12, paddingVertical: 6 },
  skipText: { color: C.textMuted, fontSize: 13, fontWeight: '500' },
  priceRow: { flexDirection: 'row', marginBottom: 10, gap: 12 },
  priceItem: {},
  priceLabel: { fontSize: 10, color: C.textSubtle, textTransform: 'uppercase', marginBottom: 2 },
  priceVal: { fontSize: 13, color: C.text, fontWeight: '500' },
  scoresRow: { flexDirection: 'row', gap: 8, borderTopWidth: 1, borderTopColor: C.border, paddingTop: 8 },
  scoreChip: { backgroundColor: C.surfaceAlt, borderRadius: 6, paddingHorizontal: 8, paddingVertical: 4, alignItems: 'center' },
  scoreLabel: { fontSize: 9, color: C.textSubtle, textTransform: 'uppercase' },
  scoreVal: { fontSize: 13, color: C.text, fontWeight: '600' },
});

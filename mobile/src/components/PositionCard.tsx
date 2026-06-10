import React, { useState } from 'react';
import {
  View, Text, StyleSheet, TouchableOpacity, Modal,
  ScrollView, TextInput, ActivityIndicator, Alert,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import { C, pnlColor } from '../theme/colors';
import { Position, EXIT_REASONS } from '../types';
import { api } from '../api/client';

interface Props {
  position: Position;
  onClosed: () => void;
}

export function PositionCard({ position: pos, onClosed }: Props) {
  const [showClose, setShowClose] = useState(false);
  const [selectedReason, setSelectedReason] = useState('');
  const [customPrice, setCustomPrice] = useState('');
  const [closing, setClosing] = useState(false);

  const pnlColor_ = pnlColor(pos.unrealised_pnl);
  const pctSign = pos.unrealised_pct >= 0 ? '+' : '';
  const isPaper = pos.is_paper;

  const groups = ['Defensive', 'Offensive', 'Other'] as const;

  const handleClose = async () => {
    if (!selectedReason) {
      Alert.alert('Select exit reason', 'Please choose an exit reason first.');
      return;
    }
    await Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
    Alert.alert(
      `Close ${pos.ticker}?`,
      `Exit reason: ${selectedReason}\nPrice: ${customPrice || pos.current_price || 'current'}`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Close Position',
          style: 'destructive',
          onPress: async () => {
            setClosing(true);
            try {
              await api.closePosition(
                pos.id,
                selectedReason,
                customPrice ? parseFloat(customPrice) : undefined,
              );
              await Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
              setShowClose(false);
              onClosed();
            } catch (e: any) {
              Alert.alert('Error', e?.response?.data?.detail || 'Failed to close position');
            } finally {
              setClosing(false);
            }
          },
        },
      ],
    );
  };

  return (
    <>
      <View style={styles.card}>
        {/* Header row */}
        <View style={styles.headerRow}>
          <View style={styles.tickerWrap}>
            <Text style={styles.ticker}>{pos.ticker}</Text>
            <View style={styles.badges}>
              {isPaper && <View style={styles.paperBadge}><Text style={styles.paperText}>PAPER</Text></View>}
              <View style={[styles.exchangeBadge, pos.asset_type === 'CRYPTO' ? styles.cryptoBadge : {}]}>
                <Text style={styles.exchangeText}>{pos.exchange_key.replace('CRYPTO_', '').replace('INDEPENDENTRESERVE', 'IR')}</Text>
              </View>
            </View>
          </View>
          <TouchableOpacity style={styles.closeBtn} onPress={() => setShowClose(true)}>
            <Text style={styles.closeBtnText}>Close ↗</Text>
          </TouchableOpacity>
        </View>

        {/* P&L row */}
        <View style={styles.pnlRow}>
          <Text style={[styles.pnl, { color: pnlColor_ }]}>
            {pos.unrealised_pnl >= 0 ? '+' : ''}${Math.abs(pos.unrealised_pnl).toFixed(2)}
          </Text>
          <Text style={[styles.pct, { color: pnlColor_ }]}>
            {pctSign}{pos.unrealised_pct.toFixed(2)}%
          </Text>
        </View>

        {/* Stats grid */}
        <View style={styles.statsGrid}>
          <View style={styles.statItem}>
            <Text style={styles.statLabel}>Entry</Text>
            <Text style={styles.statValue}>${pos.entry_price?.toFixed(4)}</Text>
          </View>
          <View style={styles.statItem}>
            <Text style={styles.statLabel}>Current</Text>
            <Text style={styles.statValue}>{pos.current_price ? `$${pos.current_price.toFixed(4)}` : '—'}</Text>
          </View>
          <View style={styles.statItem}>
            <Text style={styles.statLabel}>Stop</Text>
            <Text style={[styles.statValue, { color: C.neg }]}>${pos.current_stop?.toFixed(4)}</Text>
          </View>
          <View style={styles.statItem}>
            <Text style={styles.statLabel}>Qty</Text>
            <Text style={styles.statValue}>{pos.qty}</Text>
          </View>
        </View>

        {/* Target row */}
        {pos.target_1 && (
          <View style={styles.targetRow}>
            <Text style={styles.targetLabel}>T1 </Text>
            <Text style={[styles.targetVal, pos.target_1_hit ? { color: C.pos } : {}]}>
              ${pos.target_1.toFixed(4)}{pos.target_1_hit ? ' ✓' : ''}
            </Text>
            {pos.target_2 && (
              <>
                <Text style={styles.targetLabel}>  T2 </Text>
                <Text style={styles.targetVal}>${pos.target_2.toFixed(4)}</Text>
              </>
            )}
          </View>
        )}
      </View>

      {/* Close Position Modal */}
      <Modal visible={showClose} animationType="slide" transparent onRequestClose={() => setShowClose(false)}>
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle}>Close {pos.ticker}</Text>
              <TouchableOpacity onPress={() => setShowClose(false)}>
                <Ionicons name="close" size={22} color={C.textMuted} />
              </TouchableOpacity>
            </View>

            <ScrollView showsVerticalScrollIndicator={false}>
              {groups.map((group) => (
                <View key={group}>
                  <Text style={styles.groupLabel}>{group}</Text>
                  {EXIT_REASONS.filter((r) => r.group === group).map((r) => (
                    <TouchableOpacity
                      key={r.key}
                      style={[styles.reasonRow, selectedReason === r.key && styles.reasonSelected]}
                      onPress={() => { setSelectedReason(r.key); Haptics.selectionAsync(); }}
                    >
                      <View style={[styles.radio, selectedReason === r.key && styles.radioSelected]} />
                      <Text style={[styles.reasonText, selectedReason === r.key && { color: C.accent }]}>
                        {r.label}
                      </Text>
                    </TouchableOpacity>
                  ))}
                </View>
              ))}

              <Text style={styles.groupLabel}>Exit Price (optional)</Text>
              <TextInput
                style={styles.priceInput}
                value={customPrice}
                onChangeText={setCustomPrice}
                placeholder={`Default: ${pos.current_price || pos.entry_price}`}
                placeholderTextColor={C.textSubtle}
                keyboardType="decimal-pad"
              />
            </ScrollView>

            <TouchableOpacity
              style={[styles.confirmBtn, !selectedReason && styles.confirmBtnDisabled]}
              onPress={handleClose}
              disabled={closing || !selectedReason}
            >
              {closing ? (
                <ActivityIndicator color={C.bg} />
              ) : (
                <Text style={styles.confirmBtnText}>Confirm Close</Text>
              )}
            </TouchableOpacity>
          </View>
        </View>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: C.surface,
    borderRadius: 14,
    padding: 16,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: C.border,
  },
  headerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: 8,
  },
  tickerWrap: { flex: 1 },
  ticker: { fontSize: 18, fontWeight: '700', color: C.text },
  badges: { flexDirection: 'row', gap: 6, marginTop: 4 },
  paperBadge: { backgroundColor: '#6366f133', borderRadius: 4, paddingHorizontal: 6, paddingVertical: 2 },
  paperText: { fontSize: 10, color: '#818cf8', fontWeight: '600' },
  exchangeBadge: { backgroundColor: C.accentDim + '88', borderRadius: 4, paddingHorizontal: 6, paddingVertical: 2 },
  cryptoBadge: { backgroundColor: '#78350f88' },
  exchangeText: { fontSize: 10, color: C.textMuted, fontWeight: '600' },
  closeBtn: { backgroundColor: C.accent + '22', borderRadius: 8, paddingHorizontal: 12, paddingVertical: 6, borderWidth: 1, borderColor: C.accent + '55' },
  closeBtnText: { color: C.accent, fontSize: 13, fontWeight: '600' },
  pnlRow: { flexDirection: 'row', alignItems: 'baseline', gap: 10, marginBottom: 12 },
  pnl: { fontSize: 24, fontWeight: '700' },
  pct: { fontSize: 16, fontWeight: '600' },
  statsGrid: { flexDirection: 'row', gap: 8, marginBottom: 8 },
  statItem: { flex: 1 },
  statLabel: { fontSize: 10, color: C.textSubtle, textTransform: 'uppercase', marginBottom: 2 },
  statValue: { fontSize: 13, color: C.text, fontWeight: '500' },
  targetRow: { flexDirection: 'row', alignItems: 'center', borderTopWidth: 1, borderTopColor: C.border, paddingTop: 8, marginTop: 4 },
  targetLabel: { fontSize: 11, color: C.textMuted },
  targetVal: { fontSize: 12, color: C.textMuted },
  // Modal
  modalOverlay: { flex: 1, backgroundColor: '#00000088', justifyContent: 'flex-end' },
  modalCard: { backgroundColor: C.surface, borderTopLeftRadius: 20, borderTopRightRadius: 20, padding: 20, maxHeight: '80%' },
  modalHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 },
  modalTitle: { fontSize: 18, fontWeight: '700', color: C.text },
  groupLabel: { fontSize: 11, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.5, marginTop: 14, marginBottom: 6 },
  reasonRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 10, paddingHorizontal: 8, borderRadius: 8, gap: 10 },
  reasonSelected: { backgroundColor: C.accent + '15' },
  radio: { width: 18, height: 18, borderRadius: 9, borderWidth: 2, borderColor: C.border },
  radioSelected: { borderColor: C.accent, backgroundColor: C.accent },
  reasonText: { fontSize: 15, color: C.text },
  priceInput: { borderWidth: 1, borderColor: C.border, borderRadius: 10, padding: 12, color: C.text, backgroundColor: C.surfaceAlt, fontSize: 15, marginBottom: 12 },
  confirmBtn: { backgroundColor: C.accent, borderRadius: 12, padding: 16, alignItems: 'center', marginTop: 8 },
  confirmBtnDisabled: { backgroundColor: C.border },
  confirmBtnText: { color: C.bg, fontSize: 16, fontWeight: '700' },
});

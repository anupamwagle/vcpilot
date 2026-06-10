import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { C, pnlColor } from '../theme/colors';

interface Props {
  label: string;
  value: string;
  sub?: string;
  valueColor?: string;
  icon?: string;
}

export function StatCard({ label, value, sub, valueColor, icon }: Props) {
  return (
    <View style={styles.card}>
      <Text style={styles.label}>{icon ? `${icon}  ${label}` : label}</Text>
      <Text style={[styles.value, valueColor ? { color: valueColor } : {}]}>{value}</Text>
      {sub ? <Text style={styles.sub}>{sub}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: C.surface,
    borderRadius: 12,
    padding: 14,
    flex: 1,
    borderWidth: 1,
    borderColor: C.border,
  },
  label: {
    fontSize: 11,
    color: C.textMuted,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    marginBottom: 4,
  },
  value: {
    fontSize: 22,
    fontWeight: '700',
    color: C.text,
  },
  sub: {
    fontSize: 12,
    color: C.textMuted,
    marginTop: 2,
  },
});

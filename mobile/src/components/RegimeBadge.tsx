import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { C, regimeColor } from '../theme/colors';

interface Props {
  regime: string;
  label?: string;
  size?: 'sm' | 'md';
}

const EMOJI: Record<string, string> = {
  BULL: '🟢',
  CAUTION: '🟡',
  BEAR: '🔴',
};

export function RegimeBadge({ regime, label, size = 'md' }: Props) {
  const upper = (regime || 'UNKNOWN').toUpperCase();
  const color = regimeColor(upper);
  const emoji = EMOJI[upper] || '⚪';
  const isSmall = size === 'sm';

  return (
    <View style={[styles.badge, { borderColor: color + '66', backgroundColor: color + '18' }]}>
      <Text style={[styles.text, { color, fontSize: isSmall ? 11 : 13 }]}>
        {emoji} {label ? `${label}: ` : ''}{upper}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    borderRadius: 20,
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 4,
    alignSelf: 'flex-start',
  },
  text: {
    fontWeight: '600',
    letterSpacing: 0.3,
  },
});

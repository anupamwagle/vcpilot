import React, { useState } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, StyleSheet,
  KeyboardAvoidingView, Platform, ActivityIndicator, ScrollView, Alert,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { C } from '../theme/colors';
import { useAuth } from '../contexts/AuthContext';

export function LoginScreen() {
  const { login } = useAuth();

  const [serverURL, setServerURL] = useState('http://192.168.1.x:8501');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const handleLogin = async () => {
    if (!email || !password) {
      Alert.alert('Missing fields', 'Please enter email and password.');
      return;
    }
    if (!serverURL) {
      Alert.alert('Missing server URL', 'Enter your AstraTrade server address.');
      return;
    }
    setLoading(true);
    try {
      await login(email.trim().toLowerCase(), password, serverURL.trim());
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e?.message || 'Login failed';
      Alert.alert('Login failed', msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.root}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.inner} keyboardShouldPersistTaps="handled">
        {/* Logo / heading */}
        <View style={styles.header}>
          <Text style={styles.logo}>📈</Text>
          <Text style={styles.title}>AstraTrade</Text>
          <Text style={styles.subtitle}>Algorithmic Trading Platform</Text>
        </View>

        {/* Form */}
        <View style={styles.form}>
          <Text style={styles.label}>Email</Text>
          <TextInput
            style={styles.input}
            value={email}
            onChangeText={setEmail}
            placeholder="admin@yourorg.com"
            placeholderTextColor={C.textSubtle}
            keyboardType="email-address"
            autoCapitalize="none"
            autoCorrect={false}
          />

          <Text style={styles.label}>Password</Text>
          <TextInput
            style={styles.input}
            value={password}
            onChangeText={setPassword}
            placeholder="••••••••"
            placeholderTextColor={C.textSubtle}
            secureTextEntry
            autoCapitalize="none"
          />

          {/* Advanced toggle */}
          <TouchableOpacity onPress={() => setShowAdvanced(!showAdvanced)} style={styles.advancedToggle}>
            <Text style={styles.advancedToggleText}>
              {showAdvanced ? '▲ Hide' : '▼ Advanced'} — Server settings
            </Text>
          </TouchableOpacity>

          {showAdvanced && (
            <>
              <Text style={styles.label}>Server URL</Text>
              <TextInput
                style={styles.input}
                value={serverURL}
                onChangeText={setServerURL}
                placeholder="http://192.168.1.100:8501"
                placeholderTextColor={C.textSubtle}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
              />
              <Text style={styles.hint}>
                Use your local network IP or Cloudflare tunnel URL. Port 8501 is the default.
              </Text>
            </>
          )}

          <TouchableOpacity
            style={[styles.btn, loading && styles.btnDisabled]}
            onPress={handleLogin}
            disabled={loading}
          >
            {loading ? (
              <ActivityIndicator color={C.bg} />
            ) : (
              <Text style={styles.btnText}>Sign in →</Text>
            )}
          </TouchableOpacity>
        </View>

        <Text style={styles.footer}>AstraTrade Mobile v1.0 · astradigital.com.au</Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  inner: { flexGrow: 1, justifyContent: 'center', padding: 24 },
  header: { alignItems: 'center', marginBottom: 40 },
  logo: { fontSize: 56, marginBottom: 10 },
  title: { fontSize: 32, fontWeight: '800', color: C.text, letterSpacing: -0.5 },
  subtitle: { fontSize: 14, color: C.textMuted, marginTop: 4 },
  form: { gap: 4 },
  label: { fontSize: 12, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.5, marginTop: 14, marginBottom: 6 },
  input: {
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 12,
    padding: 14,
    fontSize: 16,
    color: C.text,
  },
  advancedToggle: { paddingVertical: 8, marginTop: 4 },
  advancedToggleText: { color: C.textMuted, fontSize: 13 },
  hint: { fontSize: 12, color: C.textSubtle, marginTop: 4 },
  btn: {
    backgroundColor: C.accent,
    borderRadius: 14,
    padding: 16,
    alignItems: 'center',
    marginTop: 24,
  },
  btnDisabled: { backgroundColor: C.border },
  btnText: { color: C.bg, fontSize: 17, fontWeight: '700' },
  footer: { textAlign: 'center', color: C.textSubtle, fontSize: 12, marginTop: 40 },
});

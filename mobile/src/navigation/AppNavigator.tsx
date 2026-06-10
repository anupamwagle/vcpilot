import React from 'react';
import { NavigationContainer } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { Ionicons } from '@expo/vector-icons';
import { View, ActivityIndicator, StyleSheet } from 'react-native';

import { useAuth } from '../contexts/AuthContext';
import { LoginScreen } from '../screens/LoginScreen';
import { DashboardScreen } from '../screens/DashboardScreen';
import { PositionsScreen } from '../screens/PositionsScreen';
import { SignalsScreen } from '../screens/SignalsScreen';
import { ActionsScreen } from '../screens/ActionsScreen';
import { C } from '../theme/colors';

const Tab = createBottomTabNavigator();

const TAB_ICONS: Record<string, { active: any; inactive: any }> = {
  Dashboard: { active: 'home',          inactive: 'home-outline' },
  Positions: { active: 'stats-chart',   inactive: 'stats-chart-outline' },
  Signals:   { active: 'radio-button-on', inactive: 'radio-button-off' },
  Actions:   { active: 'flash',         inactive: 'flash-outline' },
};

export function AppNavigator() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <View style={styles.splash}>
        <ActivityIndicator size="large" color={C.accent} />
      </View>
    );
  }

  if (!user) {
    return <LoginScreen />;
  }

  return (
    <NavigationContainer>
      <Tab.Navigator
        screenOptions={({ route }) => ({
          headerShown: false,
          tabBarStyle: styles.tabBar,
          tabBarActiveTintColor: C.accent,
          tabBarInactiveTintColor: C.textSubtle,
          tabBarLabelStyle: styles.tabLabel,
          tabBarIcon: ({ focused, color, size }) => {
            const icons = TAB_ICONS[route.name];
            const name = focused ? icons?.active : icons?.inactive;
            return <Ionicons name={name} size={22} color={color} />;
          },
        })}
      >
        <Tab.Screen name="Dashboard" component={DashboardScreen} />
        <Tab.Screen name="Positions" component={PositionsScreen} />
        <Tab.Screen name="Signals" component={SignalsScreen} />
        <Tab.Screen name="Actions" component={ActionsScreen} />
      </Tab.Navigator>
    </NavigationContainer>
  );
}

const styles = StyleSheet.create({
  splash: {
    flex: 1,
    backgroundColor: C.bg,
    justifyContent: 'center',
    alignItems: 'center',
  },
  tabBar: {
    backgroundColor: C.surface,
    borderTopColor: C.border,
    borderTopWidth: 1,
    paddingTop: 4,
    height: 60,
  },
  tabLabel: {
    fontSize: 10,
    fontWeight: '600',
    marginBottom: 4,
  },
});

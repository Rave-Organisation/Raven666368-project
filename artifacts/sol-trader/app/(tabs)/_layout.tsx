import { BlurView } from "expo-blur";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import { Tabs } from "expo-router";
import { Icon, Label, NativeTabs } from "expo-router/unstable-native-tabs";
import { SymbolView } from "expo-symbols";
import { Feather } from "@expo/vector-icons";
import React from "react";
import { Platform, StyleSheet, View, useColorScheme } from "react-native";
import { useColors } from "@/hooks/useColors";

function NativeTabLayout() {
  return (
    <NativeTabs>
      <NativeTabs.Trigger name="index">
        <Icon sf={{ default: "chart.xyaxis.line", selected: "chart.xyaxis.line" }} />
        <Label>Dashboard</Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="portfolio">
        <Icon sf={{ default: "briefcase", selected: "briefcase.fill" }} />
        <Label>Portfolio</Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="markets">
        <Icon sf={{ default: "arrow.up.arrow.down", selected: "arrow.up.arrow.down" }} />
        <Label>Markets</Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="trades">
        <Icon sf={{ default: "list.bullet.rectangle", selected: "list.bullet.rectangle.fill" }} />
        <Label>Trades</Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="mind">
        <Icon sf={{ default: "brain", selected: "brain.filled.head.profile" }} />
        <Label>Mind</Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="settings">
        <Icon sf={{ default: "gearshape", selected: "gearshape.fill" }} />
        <Label>Settings</Label>
      </NativeTabs.Trigger>
    </NativeTabs>
  );
}

function ClassicTabLayout() {
  const colors = useColors();
  const colorScheme = useColorScheme();
  const isDark = colorScheme === "dark";
  const isIOS = Platform.OS === "ios";
  const isWeb = Platform.OS === "web";

  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.mutedForeground,
        headerShown: false,
        tabBarStyle: {
          position: "absolute",
          backgroundColor: isIOS ? "transparent" : `${colors.background}F0`,
          borderTopWidth: 1,
          borderTopColor: colors.border,
          elevation: 0,
          ...(isWeb ? { height: 84 } : {}),
        },
        tabBarBackground: () =>
          isIOS ? (
            <BlurView
              intensity={90}
              tint="dark"
              style={StyleSheet.absoluteFill}
            />
          ) : (
            <View
              style={[StyleSheet.absoluteFill, { backgroundColor: `${colors.background}F0`, borderTopWidth: 1, borderTopColor: colors.border }]}
            />
          ),
        tabBarLabelStyle: {
          fontSize: 9,
          fontFamily: "Inter_700Bold",
          letterSpacing: 0.8,
          textTransform: "uppercase",
        },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "CMD",
          tabBarIcon: ({ color }) =>
            isIOS ? (
              <SymbolView name="chart.xyaxis.line" tintColor={color} size={22} />
            ) : (
              <Feather name="bar-chart-2" size={22} color={color} />
            ),
        }}
      />
      <Tabs.Screen
        name="portfolio"
        options={{
          title: "ASSETS",
          tabBarIcon: ({ color }) =>
            isIOS ? (
              <SymbolView name="briefcase" tintColor={color} size={22} />
            ) : (
              <Feather name="briefcase" size={22} color={color} />
            ),
        }}
      />
      <Tabs.Screen
        name="markets"
        options={{
          title: "RADAR",
          tabBarIcon: ({ color }) =>
            isIOS ? (
              <SymbolView name="arrow.up.arrow.down" tintColor={color} size={22} />
            ) : (
              <Feather name="trending-up" size={22} color={color} />
            ),
        }}
      />
      <Tabs.Screen
        name="trades"
        options={{
          title: "OPS LOG",
          tabBarIcon: ({ color }) =>
            isIOS ? (
              <SymbolView name="list.bullet.rectangle" tintColor={color} size={22} />
            ) : (
              <Feather name="list" size={22} color={color} />
            ),
        }}
      />
      <Tabs.Screen
        name="mind"
        options={{
          title: "AGI",
          tabBarIcon: ({ color }) =>
            isIOS ? (
              <SymbolView name="brain" tintColor={color} size={22} />
            ) : (
              <Feather name="cpu" size={22} color={color} />
            ),
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: "CONFIG",
          tabBarIcon: ({ color }) =>
            isIOS ? (
              <SymbolView name="gearshape" tintColor={color} size={22} />
            ) : (
              <Feather name="settings" size={22} color={color} />
            ),
        }}
      />
    </Tabs>
  );
}

export default function TabLayout() {
  if (isLiquidGlassAvailable()) {
    return <NativeTabLayout />;
  }
  return <ClassicTabLayout />;
}

/**
 * MilitaryBackground
 * Renders the mission-control HUD grid pattern behind all screens.
 * Layers: deep navy base → orthogonal grid → corner brackets → scanline pulse
 */
import React, { useEffect, useRef } from "react";
import { Animated, Dimensions, StyleSheet, View } from "react-native";
import { useColors } from "@/hooks/useColors";

const { width: SCREEN_W, height: SCREEN_H } = Dimensions.get("window");
const CELL = 40;
const H_LINES = Math.ceil(SCREEN_H / CELL) + 1;
const V_LINES = Math.ceil(SCREEN_W / CELL) + 1;

function GridLines({ color }: { color: string }) {
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      {Array.from({ length: H_LINES }).map((_, i) => (
        <View
          key={`h-${i}`}
          style={[
            styles.hLine,
            { top: i * CELL, backgroundColor: color },
          ]}
        />
      ))}
      {Array.from({ length: V_LINES }).map((_, i) => (
        <View
          key={`v-${i}`}
          style={[
            styles.vLine,
            { left: i * CELL, backgroundColor: color },
          ]}
        />
      ))}
    </View>
  );
}

function CornerBrackets({ color }: { color: string }) {
  const SIZE = 20;
  const THICKNESS = 2;
  const bracketStyle = { borderColor: color };
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      <View style={[styles.cornerTL, { width: SIZE, height: SIZE, borderTopWidth: THICKNESS, borderLeftWidth: THICKNESS, ...bracketStyle }]} />
      <View style={[styles.cornerTR, { width: SIZE, height: SIZE, borderTopWidth: THICKNESS, borderRightWidth: THICKNESS, ...bracketStyle }]} />
      <View style={[styles.cornerBL, { width: SIZE, height: SIZE, borderBottomWidth: THICKNESS, borderLeftWidth: THICKNESS, ...bracketStyle }]} />
      <View style={[styles.cornerBR, { width: SIZE, height: SIZE, borderBottomWidth: THICKNESS, borderRightWidth: THICKNESS, ...bracketStyle }]} />
    </View>
  );
}

function ScanlinePulse({ color }: { color: string }) {
  const translateY = useRef(new Animated.Value(-60)).current;
  const opacity    = useRef(new Animated.Value(0.6)).current;

  useEffect(() => {
    Animated.loop(
      Animated.sequence([
        Animated.parallel([
          Animated.timing(translateY, {
            toValue: SCREEN_H + 60,
            duration: 4000,
            useNativeDriver: true,
          }),
          Animated.sequence([
            Animated.timing(opacity, { toValue: 0.6, duration: 200, useNativeDriver: true }),
            Animated.timing(opacity, { toValue: 0.2, duration: 3600, useNativeDriver: true }),
            Animated.timing(opacity, { toValue: 0, duration: 200, useNativeDriver: true }),
          ]),
        ]),
        Animated.parallel([
          Animated.timing(translateY, { toValue: -60, duration: 0, useNativeDriver: true }),
          Animated.timing(opacity, { toValue: 0, duration: 0, useNativeDriver: true }),
        ]),
        Animated.delay(1000),
      ])
    ).start();
  }, []);

  return (
    <Animated.View
      pointerEvents="none"
      style={[
        styles.scanline,
        { backgroundColor: color, opacity, transform: [{ translateY }] },
      ]}
    />
  );
}

function RadarPing({ color }: { color: string }) {
  const scale   = useRef(new Animated.Value(0.3)).current;
  const opacity = useRef(new Animated.Value(0.8)).current;

  useEffect(() => {
    Animated.loop(
      Animated.sequence([
        Animated.parallel([
          Animated.timing(scale,   { toValue: 1.6, duration: 2000, useNativeDriver: true }),
          Animated.timing(opacity, { toValue: 0,   duration: 2000, useNativeDriver: true }),
        ]),
        Animated.delay(3000),
        Animated.parallel([
          Animated.timing(scale,   { toValue: 0.3, duration: 0, useNativeDriver: true }),
          Animated.timing(opacity, { toValue: 0.8, duration: 0, useNativeDriver: true }),
        ]),
      ])
    ).start();
  }, []);

  return (
    <View style={styles.pingContainer} pointerEvents="none">
      <Animated.View
        style={[
          styles.ping,
          { borderColor: color, opacity, transform: [{ scale }] },
        ]}
      />
    </View>
  );
}

interface Props {
  children: React.ReactNode;
}

export function MilitaryBackground({ children }: Props) {
  const colors = useColors();

  return (
    <View style={[styles.root, { backgroundColor: colors.background }]}>
      <GridLines color={`${colors.gridLine}60`} />
      <ScanlinePulse color={`${colors.accent}22`} />
      <RadarPing color={colors.accent} />
      <CornerBrackets color={`${colors.primary}80`} />
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
  },
  hLine: {
    position: "absolute",
    left: 0,
    right: 0,
    height: StyleSheet.hairlineWidth,
  },
  vLine: {
    position: "absolute",
    top: 0,
    bottom: 0,
    width: StyleSheet.hairlineWidth,
  },
  scanline: {
    position: "absolute",
    left: 0,
    right: 0,
    height: 60,
  },
  pingContainer: {
    position: "absolute",
    top: 80,
    right: 30,
    width: 40,
    height: 40,
    alignItems: "center",
    justifyContent: "center",
  },
  ping: {
    width: 40,
    height: 40,
    borderRadius: 20,
    borderWidth: 1.5,
  },
  cornerTL: { position: "absolute", top: 8, left: 8 },
  cornerTR: { position: "absolute", top: 8, right: 8 },
  cornerBL: { position: "absolute", bottom: 8, left: 8 },
  cornerBR: { position: "absolute", bottom: 8, right: 8 },
});

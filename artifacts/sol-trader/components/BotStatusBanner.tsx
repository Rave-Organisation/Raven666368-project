import { Feather } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import React, { useEffect, useRef } from "react";
import { Animated, StyleSheet, Text, TouchableOpacity, View } from "react-native";
import { useColors } from "@/hooks/useColors";
import { useTrading } from "@/context/TradingContext";

const STATUS_CONFIG: Record<string, { label: string; icon: string; color: string | null }> = {
  running: { label: "SYSTEMS ONLINE — REGIME ACTIVE", icon: "activity", color: null },
  zen_mode: { label: "STANDBY — LOW SIGNAL ENVIRONMENT", icon: "moon", color: null },
  resting: { label: "REST CYCLE — RECHARGING SYSTEMS", icon: "moon", color: null },
  stopped: { label: "SYSTEMS OFFLINE", icon: "pause-circle", color: null },
  paused: { label: "EXECUTION PAUSED", icon: "pause-circle", color: null },
  error: { label: "SYSTEM FAULT — CHECK LOGS", icon: "alert-circle", color: null },
};

export function BotStatusBanner() {
  const colors = useColors();
  const { botStatus, toggleBot, config, astralSensor, restCycle } = useTrading();
  const pulseAnim = useRef(new Animated.Value(1)).current;
  const isRunning = botStatus === "running";
  const isZen = botStatus === "zen_mode";
  const isResting = botStatus === "resting";

  useEffect(() => {
    if (isRunning) {
      const pulse = Animated.loop(
        Animated.sequence([
          Animated.timing(pulseAnim, { toValue: 1.3, duration: 1000, useNativeDriver: true }),
          Animated.timing(pulseAnim, { toValue: 1, duration: 1000, useNativeDriver: true }),
        ])
      );
      pulse.start();
      return () => pulse.stop();
    } else {
      pulseAnim.setValue(1);
    }
  }, [isRunning]);

  const handleToggle = () => {
    if (isResting) return;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    toggleBot();
  };

  const statusColor = isRunning
    ? colors.chartUp
    : isZen
    ? colors.warning
    : isResting
    ? colors.info
    : colors.mutedForeground;

  const statusBg = isRunning
    ? `${colors.chartUp}12`
    : isZen
    ? `${colors.warning}10`
    : isResting
    ? `${colors.info}10`
    : colors.card;

  const borderColor = isRunning
    ? `${colors.chartUp}40`
    : isZen
    ? `${colors.warning}40`
    : isResting
    ? `${colors.info}40`
    : colors.border;

  const cfg = STATUS_CONFIG[botStatus] || STATUS_CONFIG.stopped;

  return (
    <View style={[styles.container, { backgroundColor: statusBg, borderColor }]}>
      <View style={styles.left}>
        <View style={styles.dotContainer}>
          {isRunning && (
            <Animated.View
              style={[
                styles.dotRing,
                { borderColor: statusColor, transform: [{ scale: pulseAnim }] },
              ]}
            />
          )}
          <View style={[styles.dot, { backgroundColor: statusColor }]} />
        </View>
        <View>
          <Text style={[styles.statusText, { color: colors.foreground }]}>{cfg.label}</Text>
          <Text style={[styles.strategyText, { color: colors.mutedForeground }]}>
            {isResting
              ? `Wakes in ${Math.ceil(((restCycle?.endsAt || 0) - Date.now()) / 3600000)}h`
              : isZen
              ? `Freq ${(astralSensor.frequency * 100).toFixed(0)}% — needs ${(config.minAstralFrequency * 100).toFixed(0)}%+`
              : `${config.strategy} · ${config.enabledTokens.slice(0, 3).join(", ")}`}
          </Text>
        </View>
      </View>
      <TouchableOpacity
        onPress={handleToggle}
        disabled={isResting}
        style={[
          styles.toggleBtn,
          {
            backgroundColor: isRunning
              ? `${colors.chartDown}20`
              : isResting
              ? `${colors.mutedForeground}10`
              : `${colors.chartUp}20`,
            borderColor: isRunning
              ? `${colors.chartDown}50`
              : isResting
              ? colors.border
              : `${colors.chartUp}50`,
          },
        ]}
        activeOpacity={0.7}
      >
        <Feather
          name={isRunning ? "pause" : isResting ? "moon" : "play"}
          size={16}
          color={
            isRunning
              ? colors.chartDown
              : isResting
              ? colors.mutedForeground
              : colors.chartUp
          }
        />
        <Text
          style={[
            styles.toggleText,
            {
              color: isRunning
                ? colors.chartDown
                : isResting
                ? colors.mutedForeground
                : colors.chartUp,
            },
          ]}
        >
          {isRunning ? "Pause" : isResting ? "Resting" : "Start"}
        </Text>
      </TouchableOpacity>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    padding: 14,
    borderRadius: 12,
    borderWidth: 1,
    marginHorizontal: 20,
    marginVertical: 8,
  },
  left: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    flex: 1,
  },
  dotContainer: {
    width: 16,
    height: 16,
    alignItems: "center",
    justifyContent: "center",
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    position: "absolute",
  },
  dotRing: {
    width: 16,
    height: 16,
    borderRadius: 8,
    borderWidth: 1.5,
    position: "absolute",
  },
  statusText: {
    fontSize: 14,
    fontFamily: "Inter_600SemiBold",
  },
  strategyText: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
    marginTop: 2,
    textTransform: "capitalize",
  },
  toggleBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 8,
    borderWidth: 1,
  },
  toggleText: {
    fontSize: 13,
    fontFamily: "Inter_600SemiBold",
  },
});

import { Feather } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import React from "react";
import {
  Alert,
  Platform,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { AGIReasoningLog } from "@/components/AGIReasoningLog";
import { AstralFrequencyMeter } from "@/components/AstralFrequencyMeter";
import { BotStatusBanner } from "@/components/BotStatusBanner";
import { EngineStatusPanel } from "@/components/EngineStatusPanel";
import { FlipLadder } from "@/components/FlipLadder";
import { PriceChart } from "@/components/PriceChart";
import { RestCycleOverlay } from "@/components/RestCycleOverlay";
import { SignalItem } from "@/components/SignalItem";
import { StatCard } from "@/components/StatCard";
import { useTrading } from "@/context/TradingContext";
import { useColors } from "@/hooks/useColors";

export default function DashboardScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const {
    portfolio, prices, signals, totalProfitToday, totalTradesCount,
    winRate, refreshPrices, astralSensor, flipLadder, restCycle,
    config, triggerGreedKill, forceRest, wakeFromRest,
    marketHealthIndex, engineController, agiReasoningLog,
    emergencyHalt, resumeEngine, marketStructure,
  } = useTrading();

  const [refreshing, setRefreshing] = React.useState(false);
  const solPrice = prices["SOL"];
  const topPaddingWeb = Platform.OS === "web" ? 67 : 0;
  const bottomPaddingWeb = Platform.OS === "web" ? 34 : 0;

  const onRefresh = async () => {
    setRefreshing(true);
    refreshPrices();
    setTimeout(() => setRefreshing(false), 800);
  };

  const handleEmergencyHalt = () => {
    Alert.alert("Emergency Protocol", "Halt all trading systems?", [
      { text: "Cancel", style: "cancel" },
      { text: "HALT ALL SYSTEMS", style: "destructive", onPress: () => { Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error); emergencyHalt(); } },
    ]);
  };

  const regimeColor = marketStructure.regime === "expansion" ? colors.chartUp
    : marketStructure.regime === "contraction" ? colors.chartDown
    : colors.warning;

  return (
    <ScrollView
      style={[styles.container, { backgroundColor: colors.background }]}
      contentContainerStyle={[
        styles.content,
        { paddingTop: topPaddingWeb + insets.top + 16, paddingBottom: bottomPaddingWeb + 100 },
      ]}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.primary} />}
      showsVerticalScrollIndicator={false}
    >
      <View style={styles.headerRow}>
        <View>
          <Text style={[styles.greeting, { color: colors.primary }]}>▸ COMMAND CENTER</Text>
          <Text style={[styles.totalValue, { color: colors.foreground }]}>
            ${portfolio.totalValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </Text>
          <Text style={[styles.pnlText, { color: portfolio.totalPnl >= 0 ? colors.chartUp : colors.chartDown }]}>
            {portfolio.totalPnl >= 0 ? "+" : ""}${portfolio.totalPnl.toFixed(2)} ({portfolio.totalPnlPercent >= 0 ? "+" : ""}{portfolio.totalPnlPercent.toFixed(2)}%)
          </Text>
          {portfolio.coldVault > 0 && (
            <Text style={[styles.vaultText, { color: colors.chartUp }]}>+ ${portfolio.coldVault.toFixed(2)} cold vault</Text>
          )}
        </View>
        <View style={styles.headerBadges}>
          <View style={[styles.mhiBadge, {
            backgroundColor: marketHealthIndex.score >= 60 ? `${colors.chartUp}15` : `${colors.warning}15`,
            borderColor: marketHealthIndex.score >= 60 ? `${colors.chartUp}40` : `${colors.warning}40`,
          }]}>
            <Text style={[styles.mhiLabel, { color: colors.mutedForeground }]}>MHI</Text>
            <Text style={[styles.mhiScore, { color: marketHealthIndex.score >= 60 ? colors.chartUp : colors.warning }]}>
              {marketHealthIndex.score.toFixed(0)}
            </Text>
          </View>
          <View style={[styles.regimeBadge, { backgroundColor: `${regimeColor}15`, borderColor: `${regimeColor}40` }]}>
            <Text style={[styles.regimeLabel, { color: colors.mutedForeground }]}>Regime</Text>
            <Text style={[styles.regimeValue, { color: regimeColor }]}>{marketStructure.regime.slice(0,3).toUpperCase()}</Text>
          </View>
        </View>
      </View>

      {solPrice && (
        <View style={[styles.solCard, { backgroundColor: colors.card, borderColor: colors.border }]}>
          <View style={styles.solCardHeader}>
            <View>
              <Text style={[styles.solSymbol, { color: colors.mutedForeground }]}>SOL / USD</Text>
              <Text style={[styles.solPrice, { color: colors.foreground }]}>
                ${solPrice.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </Text>
            </View>
            <View style={styles.solChangeCol}>
              <Text style={[styles.solChange, { color: solPrice.change24h >= 0 ? colors.chartUp : colors.chartDown }]}>
                {solPrice.change24h >= 0 ? "+" : ""}{solPrice.change24h.toFixed(2)}%
              </Text>
              <Text style={[styles.solChangeLabel, { color: colors.mutedForeground }]}>24h</Text>
            </View>
          </View>
          <PriceChart symbol="SOL" currentPrice={solPrice.price} change24h={solPrice.change24h} height={70} />
        </View>
      )}

      {restCycle?.active ? (
        <RestCycleOverlay cycle={restCycle} onWake={wakeFromRest} />
      ) : (
        <BotStatusBanner />
      )}

      <View style={styles.statsGrid}>
        <StatCard label="Today P&L" value={`${totalProfitToday >= 0 ? "+" : ""}$${Math.abs(totalProfitToday).toFixed(2)}`}
          trend={totalProfitToday >= 0 ? "up" : "down"} compact />
        <StatCard label="Win Rate" value={`${isNaN(winRate) ? "—" : winRate.toFixed(0)}%`}
          trend={winRate >= 55 ? "up" : winRate < 45 ? "down" : "neutral"} compact />
      </View>
      <View style={styles.statsGrid}>
        <StatCard label="Total Trades" value={totalTradesCount.toString()} compact />
        <StatCard label="USDC Balance" value={`$${portfolio.usdcBalance.toFixed(0)}`} compact />
      </View>

      <View style={styles.padded}>
        <EngineStatusPanel
          engine={engineController}
          onHalt={handleEmergencyHalt}
          onResume={() => { Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light); resumeEngine(); }}
        />
      </View>

      <View style={styles.padded}>
        <AstralFrequencyMeter sensor={astralSensor} minFreq={config.minAstralFrequency} compact />
      </View>

      <View style={styles.padded}>
        <FlipLadder
          ladder={flipLadder}
          greedKillEnabled={config.greedKillEnabled}
          onGreedKill={() => { Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning); triggerGreedKill(); }}
          onForceRest={(h) => { Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); forceRest(h); }}
        />
      </View>

      {agiReasoningLog.length > 0 && (
        <View style={styles.padded}>
          <AGIReasoningLog entries={agiReasoningLog} compact />
        </View>
      )}

      {signals.length > 0 && (
        <View style={styles.section}>
          <Text style={[styles.sectionTitle, { color: colors.accent }]}>▸ LIVE SIGNALS</Text>
          {signals.slice(0, 6).map((sig) => <SignalItem key={sig.id} signal={sig} />)}
        </View>
      )}

      {signals.length === 0 && (
        <View style={[styles.emptySignals, { borderColor: colors.border }]}>
          <Feather name="activity" size={32} color={colors.mutedForeground} />
          <Text style={[styles.emptyText, { color: colors.mutedForeground }]}>Start the bot to see live signals</Text>
        </View>
      )}

      <TouchableOpacity
        onPress={handleEmergencyHalt}
        style={[styles.emergencyBtn, {
          backgroundColor: engineController.circuitBreaker.triggered ? `${colors.mutedForeground}10` : `${colors.chartDown}10`,
          borderColor: engineController.circuitBreaker.triggered ? colors.border : `${colors.chartDown}50`,
        }]}
        activeOpacity={0.7}
      >
        <Feather name="alert-octagon" size={16} color={engineController.circuitBreaker.triggered ? colors.mutedForeground : colors.chartDown} />
        <Text style={[styles.emergencyText, { color: engineController.circuitBreaker.triggered ? colors.mutedForeground : colors.chartDown }]}>
          {engineController.circuitBreaker.triggered ? "ENGINE HALTED — TAP TO RESUME IN MIND TAB" : "EMERGENCY PROTOCOL: HALT ALL SYSTEMS"}
        </Text>
      </TouchableOpacity>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  content: { gap: 0 },
  headerRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "flex-start", paddingHorizontal: 20, marginBottom: 20 },
  greeting: { fontSize: 13, fontFamily: "Inter_500Medium", letterSpacing: 0.5, textTransform: "uppercase", marginBottom: 4 },
  totalValue: { fontSize: 34, fontFamily: "Inter_700Bold", letterSpacing: -1 },
  pnlText: { fontSize: 13, fontFamily: "Inter_500Medium", marginTop: 4 },
  vaultText: { fontSize: 12, fontFamily: "Inter_600SemiBold", marginTop: 2 },
  headerBadges: { gap: 6 },
  mhiBadge: { alignItems: "center", paddingHorizontal: 12, paddingVertical: 6, borderRadius: 10, borderWidth: 1, gap: 1 },
  mhiLabel: { fontSize: 10, fontFamily: "Inter_500Medium", letterSpacing: 0.5 },
  mhiScore: { fontSize: 20, fontFamily: "Inter_700Bold", letterSpacing: -1 },
  regimeBadge: { alignItems: "center", paddingHorizontal: 12, paddingVertical: 6, borderRadius: 10, borderWidth: 1, gap: 1 },
  regimeLabel: { fontSize: 10, fontFamily: "Inter_500Medium", letterSpacing: 0.5 },
  regimeValue: { fontSize: 14, fontFamily: "Inter_700Bold", letterSpacing: 0.5 },
  solCard: { marginHorizontal: 20, borderRadius: 16, borderWidth: 1, padding: 16, marginBottom: 8, gap: 12 },
  solCardHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "flex-start" },
  solSymbol: { fontSize: 12, fontFamily: "Inter_500Medium", opacity: 0.6, marginBottom: 4 },
  solPrice: { fontSize: 26, fontFamily: "Inter_700Bold", letterSpacing: -0.5 },
  solChangeCol: { alignItems: "flex-end" },
  solChange: { fontSize: 18, fontFamily: "Inter_700Bold" },
  solChangeLabel: { fontSize: 11, fontFamily: "Inter_400Regular", marginTop: 2 },
  statsGrid: { flexDirection: "row", paddingHorizontal: 20, gap: 10, marginBottom: 10, marginTop: 8 },
  padded: { paddingHorizontal: 20, marginBottom: 10 },
  section: { marginTop: 8 },
  sectionTitle: { fontSize: 18, fontFamily: "Inter_700Bold", marginBottom: 12, paddingHorizontal: 20 },
  emptySignals: { marginHorizontal: 20, marginTop: 24, borderWidth: 1, borderStyle: "dashed", borderRadius: 12, padding: 32, alignItems: "center", gap: 12 },
  emptyText: { fontSize: 14, fontFamily: "Inter_400Regular", textAlign: "center" },
  emergencyBtn: { flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8, marginHorizontal: 20, marginTop: 12, padding: 14, borderRadius: 10, borderWidth: 1 },
  emergencyText: { fontSize: 11, fontFamily: "Inter_700Bold", letterSpacing: 0.8, textTransform: "uppercase", textAlign: "center", flex: 1 },
});

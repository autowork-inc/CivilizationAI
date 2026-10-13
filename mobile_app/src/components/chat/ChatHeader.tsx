import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import { ActivityIndicator, Pressable, Text, View } from 'react-native';

type Props = {
  title: string;
  subtitle?: string;
  onLogout: () => void;
  loggingOut?: boolean;
};

/** Top bar: persona avatar + name on the left, a compact "Выйти" pill on the right. */
export function ChatHeader({ title, subtitle, onLogout, loggingOut = false }: Props) {
  return (
    <View className="flex-row items-center border-b border-slate-800 bg-slate-900 px-4 py-3">
      <LinearGradient
        colors={['#3b82f6', '#6366f1']}
        start={{ x: 0, y: 0 }}
        end={{ x: 1, y: 1 }}
        style={{ width: 40, height: 40, borderRadius: 14, alignItems: 'center', justifyContent: 'center' }}>
        <Ionicons name="sparkles" size={20} color="#ffffff" />
      </LinearGradient>

      <View className="ml-3 flex-1">
        <Text numberOfLines={1} className="text-base font-semibold text-slate-50">
          {title}
        </Text>
        {subtitle ? (
          <Text numberOfLines={1} className="text-xs text-slate-400">
            {subtitle}
          </Text>
        ) : null}
      </View>

      <Pressable
        onPress={onLogout}
        disabled={loggingOut}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel="Выйти"
        className={`h-9 flex-row items-center rounded-full border border-slate-700 bg-slate-800 px-3.5 active:bg-slate-700 ${
          loggingOut ? 'opacity-60' : ''
        }`}>
        {loggingOut ? (
          <ActivityIndicator size="small" color="#cbd5e1" />
        ) : (
          <>
            <Ionicons name="log-out-outline" size={16} color="#cbd5e1" />
            <Text className="ml-1.5 text-sm font-medium text-slate-300">Выйти</Text>
          </>
        )}
      </Pressable>
    </View>
  );
}

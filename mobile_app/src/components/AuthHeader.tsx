import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import { Text, View } from 'react-native';

type Props = { title: string; subtitle: string };

/** Brand block for the auth screens: gradient logo badge, accent-coloured wordmark, screen title. */
export function AuthHeader({ title, subtitle }: Props) {
  return (
    <View className="mb-10">
      <View className="mb-8 flex-row items-center">
        <LinearGradient
          colors={['#3b82f6', '#6366f1']}
          start={{ x: 0, y: 0 }}
          end={{ x: 1, y: 1 }}
          style={{ width: 44, height: 44, borderRadius: 14, alignItems: 'center', justifyContent: 'center' }}>
          <Ionicons name="sparkles" size={22} color="#ffffff" />
        </LinearGradient>
        <Text className="ml-3 text-xl font-bold tracking-tight text-slate-50">
          Civilization <Text className="text-indigo-400">AI</Text>
        </Text>
      </View>
      <Text className="text-4xl font-extrabold tracking-tight text-slate-50">{title}</Text>
      <Text className="mt-3 text-base leading-6 text-slate-400">{subtitle}</Text>
    </View>
  );
}
